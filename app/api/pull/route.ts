// /app/api/pull/route.ts

import { Redis } from '@upstash/redis';
import { Client } from '@notionhq/client';
import { NextRequest } from 'next/server';
import { convert } from 'html-to-text';
import type {
  CreatePageParameters,
} from '@notionhq/client/build/src/api-endpoints';

// =======================================================
//   *** 在这里修改每次导入的文章数量 ***
const MAX_ARTICLES_PER_RUN = 20; // 推荐设置为 20-30
// =======================================================

// --- 类型定义 ---
interface TokenData {
  accessToken: string;
  refreshToken:string;
  expiresAt: number;
}
interface InoreaderItem {
  id: string;
  title?: string;
  canonical?: Array<{ href: string }>;
  alternate?: Array<{ href:string }>;
  summary?: { content: string };
  origin?: { title: string };
}
interface InoreaderResponse {
  items?: InoreaderItem[];
}
interface SyncResult {
  success: boolean;
  message: string;
  imported: number;
  skipped: number;
}

// --- 客户端初始化 ---
const redis = new Redis({
  url: process.env.KV_REST_API_URL!,
  token: process.env.KV_REST_API_TOKEN!,
});
const notion = new Client({
  auth: process.env.NOTION_TOKEN,
});

// --- HTML 清洗和文本分割函数 ---
function cleanHtmlContent(html: string | undefined | null): string {
  if (!html) return '';
  return convert(html, {
    wordwrap: false,
    selectors: [
      { selector: 'img', format: 'skip' },
      { selector: 'a', options: { ignoreHref: true } },
    ],
  }).replace(/\n\s*\n/g, '\n');
}

function splitTextForRichText(text: string, chunkSize = 1800): string[] {
    const chunks: string[] = [];
    if (!text) return chunks;
    let currentPos = 0;
    while (currentPos < text.length) {
        chunks.push(text.substring(currentPos, currentPos + chunkSize));
        currentPos += chunkSize;
    }
    return chunks;
}

// --- 刷新 Inoreader 令牌函数 ---
async function refreshInoreaderToken(refreshToken: string): Promise<string> {
    try {
        const clientId = process.env.INOREADER_CLIENT_ID;
        const clientSecret = process.env.INOREADER_CLIENT_SECRET;
        if (!clientId || !clientSecret) throw new Error('Inoreader 客户端ID或密钥未配置');
        const response = await fetch('https://www.inoreader.com/oauth2/token', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: new URLSearchParams({ client_id: clientId, client_secret: clientSecret, grant_type: 'refresh_token', refresh_token: refreshToken })
        });
        if (!response.ok) throw new Error(`令牌刷新失败: ${response.status}`);
        const tokenData: any = await response.json();
        if (typeof tokenData.access_token !== 'string') throw new Error('刷新后未收到有效的access_token');
        const newTokenData: TokenData = {
            accessToken: tokenData.access_token,
            refreshToken: tokenData.refresh_token,
            expiresAt: Date.now() + (tokenData.expires_in * 1000)
        };
        await redis.set('inoreader_tokens', JSON.stringify(newTokenData));
        return newTokenData.accessToken;
    } catch (error) {
        console.error(`令牌刷新失败: ${(error as Error).message}`);
        throw error;
    }
}

// --- 核心同步函数 (使用“永远最新”策略) ---
async function syncInoreaderToNotion(): Promise<SyncResult> {
  const syncId = `sync_${Date.now().toString().slice(-6)}`;
  console.log(`[${syncId}] 开始同步任务`);
  
  try {
    // 1. 获取并刷新令牌
    const tokenDataFromRedis: unknown = await redis.get('inoreader_tokens');
    if (!tokenDataFromRedis || typeof tokenDataFromRedis !== 'object') throw new Error('未在Redis中找到令牌对象，请先授权');
    let tokenData = tokenDataFromRedis as TokenData;
    let accessToken = tokenData.accessToken;
    if (Date.now() > tokenData.expiresAt) {
      console.log(`[${syncId}] 令牌已过期，正在刷新...`);
      accessToken = await refreshInoreaderToken(tokenData.refreshToken);
    }

    // 2. 拉取最新的文章列表
    // Inoreader 默认按加星时间倒序，最新的永远在最前面
    const apiUrl = `https://www.inoreader.com/reader/api/0/stream/contents/user/-/state/com.google/starred?n=100`; // 一次性拉取足够多的文章
    const starredItemsResponse = await fetch(apiUrl, { headers: { 'Authorization': `Bearer ${accessToken}` } });
    if (!starredItemsResponse.ok) throw new Error(`拉取文章失败: ${starredItemsResponse.status}`);
    
    const starredData: InoreaderResponse = await starredItemsResponse.json();
    const allItems = Array.isArray(starredData.items) ? starredData.items : [];
    
    // 从最新的文章里，截取一个批次进行处理
    const itemsToProcess = allItems.slice(0, MAX_ARTICLES_PER_RUN);
    console.log(`[${syncId}] 拉取到 ${allItems.length} 篇文章, 准备处理最新的 ${itemsToProcess.length} 篇`);

    // 3. 同步到 Notion
    const databaseId = process.env.NOTION_DATABASE_ID!;
    let importedCount = 0;
    let skippedCount = 0;

    for (const item of itemsToProcess) {
      const articleUrl = (item.canonical?.[0]?.href) || (item.alternate?.[0]?.href);
      if (!articleUrl) {
        skippedCount++;
        continue;
      }
      
      try {
        const existingPages = await notion.databases.query({ database_id: databaseId, filter: { property: '网址', url: { equals: articleUrl } } });
        if (existingPages.results.length > 0) {
          skippedCount++;
          continue;
        }
      } catch (queryError) {
        console.error(`[${syncId}] 检查文章存在性失败 for "${item.title}": ${(queryError as Error).message}`);
        skippedCount++;
        continue; 
      }
      
      const pageTitle = item.title?.trim() || '无标题文章';
      const sourceName = item.origin?.title?.trim() || '未知来源';
      
      const fullContent = cleanHtmlContent(item.summary?.content);
      const contentChunks = splitTextForRichText(fullContent);

      const contentForNotionProperty = contentChunks.map(chunk => ({
        type: 'text' as const,
        text: { content: chunk }
      }));

      try {
        const pageData: CreatePageParameters = {
          parent: { database_id: databaseId },
          properties: {
            '文章名称': { title: [{ type: 'text', text: { content: pageTitle } }] },
            '网址': { url: articleUrl },
            '作者': { rich_text: [{ type: 'text', text: { content: sourceName } }] },
            '内容': { rich_text: contentForNotionProperty }
          },
          children: []
        };

        await notion.pages.create(pageData);
        console.log(`[${syncId}] 成功导入: ${pageTitle.substring(0, 30)}...`);
        importedCount++;
      } catch (createError) {
        console.error(`[${syncId}] 创建页面失败 for "${pageTitle}": ${(createError as Error).message}`);
        skippedCount++;
      }
    }
    
    return { success: true, message: `同步完成, 成功导入${importedCount}篇, 跳过${skippedCount}篇`, imported: importedCount, skipped: skippedCount };
  } catch (error) {
    console.error(`[${syncId}] 同步任务失败: ${(error as Error).message}`);
    throw error;
  }
}

// --- 请求处理 (保持安全验证) ---
export async function GET(request: NextRequest): Promise<Response> {
    const authHeader = request.headers.get('authorization');
    if (authHeader !== `Bearer ${process.env.CRON_SECRET}`) {
        return new Response('Unauthorized', { status: 401 });
    }

    try {
        const result = await syncInoreaderToNotion();
        return new Response(JSON.stringify(result), { status: 200, headers: { 'Content-Type': 'application/json' } });
    } catch (error) {
        return new Response(JSON.stringify({ success: false, message: (error as Error).message }), { status: 500, headers: { 'Content-Type': 'application/json' } });
    }
}

export async function POST(request: NextRequest): Promise<Response> {
    const authHeader = request.headers.get('authorization');
    if (authHeader !== `Bearer ${process.env.CRON_SECRET}`) {
        return new Response('Unauthorized', { status: 401 });
    }
    
    try {
        const result = await syncInoreaderToNotion();
        return new Response(JSON.stringify(result), { status: 200, headers: { 'Content-Type': 'application/json' } });
    } catch (error) {
        return new Response(JSON.stringify({ success: false, message: (error as Error).message }), { status: 500, headers: { 'Content-Type': 'application/json' } });
    }
}