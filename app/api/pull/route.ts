// /app/api/pull/route.ts

import { Redis } from '@upstash/redis';
import { Client } from '@notionhq/client';
import { NextRequest } from 'next/server';
import { convert } from 'html-to-text'; // <-- 1. 引入新安装的库
import type {
  CreatePageParameters,
} from '@notionhq/client/build/src/api-endpoints';

// --- 类型定义 (保持不变) ---
interface TokenData {
  accessToken: string;
  refreshToken: string;
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

// --- 客户端初始化 (保持不变) ---
const redis = new Redis({
  url: process.env.KV_REST_API_URL!,
  token: process.env.KV_REST_API_TOKEN!,
});
const notion = new Client({
  auth: process.env.NOTION_TOKEN,
});

// --- 新增的 HTML 清洗函数 ---
/**
 * 将HTML字符串转换为纯文本
 * @param html - 包含HTML标签的字符串
 * @returns 清理后的纯文本字符串
 */
function cleanHtmlContent(html: string | undefined | null): string {
  if (!html) {
    return '';
  }
  return convert(html, {
    wordwrap: false, // 不自动换行
    selectors: [
      { selector: 'img', format: 'skip' }, // 忽略所有图片
      { selector: 'a', options: { ignoreHref: true } }, // 保留链接文本，但丢弃链接地址
    ],
  });
}


// --- refreshInoreaderToken 函数 (保持不变) ---
async function refreshInoreaderToken(refreshToken: string): Promise<string> {
    // ... 此函数代码无需修改，保持原样 ...
    const functionId = `refresh_${Date.now().toString().slice(-6)}`;
    console.log(`[${new Date().toISOString()}] [${functionId}] 开始刷新令牌`);
    try {
        const clientId = process.env.INOREADER_CLIENT_ID;
        const clientSecret = process.env.INOREADER_CLIENT_SECRET;
        if (!clientId || !clientSecret) {
            throw new Error('Inoreader 客户端ID或密钥未配置');
        }
        const response = await fetch('https://www.inoreader.com/oauth2/token', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: new URLSearchParams({
                client_id: clientId,
                client_secret: clientSecret,
                grant_type: 'refresh_token',
                refresh_token: refreshToken
            })
        });
        if (!response.ok) {
            throw new Error(`令牌刷新失败: ${response.status}`);
        }
        const tokenData: any = await response.json();
        if (typeof tokenData.access_token !== 'string') {
            throw new Error('刷新后未收到有效的access_token');
        }
        const newTokenData: TokenData = {
            accessToken: tokenData.access_token,
            refreshToken: tokenData.refresh_token,
            expiresAt: Date.now() + (tokenData.expires_in * 1000)
        };
        await redis.set('inoreader_tokens', JSON.stringify(newTokenData));
        console.log(`[${new Date().toISOString()}] [${functionId}] 令牌刷新并保存成功`);
        return newTokenData.accessToken;
    } catch (error) {
        console.error(`[${new Date().toISOString()}] [${functionId}] 令牌刷新失败: ${(error as Error).message}`);
        throw error;
    }
}


// --- syncInoreaderToNotion 函数 (这是被修改的部分) ---
async function syncInoreaderToNotion(): Promise<SyncResult> {
  const syncId = `sync_${Date.now().toString().slice(-6)}`;
  console.log(`[${new Date().toISOString()}] [${syncId}] 开始同步任务`);
  
  try {
    // 1. 获取和验证令牌 (保持不变)
    console.log(`[${new Date().toISOString()}] [${syncId}] 步骤1/3: 获取Inoreader令牌`);
    const tokenDataFromRedis: unknown = await redis.get('inoreader_tokens');
    if (!tokenDataFromRedis || typeof tokenDataFromRedis !== 'object') {
      throw new Error('未在Redis中找到令牌对象，请先授权');
    }
    let tokenData = tokenDataFromRedis as TokenData;
    let accessToken = tokenData.accessToken;
    if (Date.now() > tokenData.expiresAt) {
      accessToken = await refreshInoreaderToken(tokenData.refreshToken);
    }

    // 2. 拉取Inoreader文章 (保持不变)
    console.log(`[${new Date().toISOString()}] [${syncId}] 步骤2/3: 拉取Inoreader星标文章`);
    const starredItemsResponse = await fetch(
        'https://www.inoreader.com/reader/api/0/stream/contents/user/-/state/com.google/starred',
        { headers: { 'Authorization': `Bearer ${accessToken}` } }
    );
    if (!starredItemsResponse.ok) {
        throw new Error(`拉取文章失败: ${starredItemsResponse.status}`);
    }
    const starredData: InoreaderResponse = await starredItemsResponse.json();
    const items = Array.isArray(starredData.items) ? starredData.items : [];
    const itemsToImport = items.slice(0, 10);
    console.log(`[${new Date().toISOString()}] [${syncId}] 拉取到${items.length}篇文章, 准备处理前${itemsToImport.length}篇`);

    // 3. 同步到Notion数据库
    console.log(`[${new Date().toISOString()}] [${syncId}] 步骤3/3: 同步到Notion`);
    const databaseId = process.env.NOTION_DATABASE_ID!;

    let importedCount = 0;
    let skippedCount = 0;

    for (const item of itemsToImport) {
      const articleUrl = (item.canonical?.[0]?.href) || (item.alternate?.[0]?.href);
      if (!articleUrl) {
        skippedCount++;
        continue;
      }

      try {
        const existingPages = await notion.databases.query({
          database_id: databaseId,
          filter: { property: '网址', url: { equals: articleUrl } }
        });
        if (existingPages.results.length > 0) {
          skippedCount++;
          continue;
        }
      } catch (queryError) {
        console.error(`[${new Date().toISOString()}] [${syncId}] 检查文章存在性失败: ${(queryError as Error).message}`);
        skippedCount++;
        continue; 
      }
      
      const pageTitle = item.title?.trim() || '无标题文章';
      const sourceName = item.origin?.title?.trim() || '未知来源';
      
      // *** 关键修改：先清洗HTML，再截断 ***
      const rawHtmlContent = item.summary?.content;
      const cleanedContent = cleanHtmlContent(rawHtmlContent); // 调用清洗函数
      const finalContent = cleanedContent.substring(0, 1800);  // 对清洗后的纯文本进行截断

      try {
        const pageData: CreatePageParameters = {
          parent: { database_id: databaseId },
          properties: {
            '文章名称': { title: [{ type: 'text', text: { content: pageTitle } }] },
            '网址': { url: articleUrl },
            '作者': { rich_text: [{ type: 'text', text: { content: sourceName } }] },
            '内容': { rich_text: [{ type: 'text', text: { content: finalContent } }] } // 使用清洗和截断后的内容
          }
        };

        await notion.pages.create(pageData);
        console.log(`[${new Date().toISOString()}] [${syncId}] 成功导入: ${pageTitle.substring(0, 30)}...`);
        importedCount++;
      } catch (createError) {
        console.error(`[${new Date().toISOString()}] [${syncId}] 创建页面失败 for "${pageTitle}": ${(createError as Error).message}`);
        skippedCount++;
      }
    }
    
    const resultMessage = `同步完成, 成功导入${importedCount}篇, 跳过${skippedCount}篇`;
    console.log(`[${new Date().toISOString()}] [${syncId}] ${resultMessage}`);
    return { success: true, message: resultMessage, imported: importedCount, skipped: skippedCount };
  } catch (error) {
    console.error(`[${new Date().toISOString()}] [${syncId}] 同步任务失败: ${(error as Error).message}`);
    throw error;
  }
}

// --- 请求处理 (保持不变) ---
export async function GET(request: NextRequest): Promise<Response> {
  // ... 此函数代码无需修改，保持原样 ...
  try {
    const result = await syncInoreaderToNotion();
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: { 'Content-Type': 'application/json' }
    });
  } catch (error) {
    return new Response(JSON.stringify({
      success: false,
      message: (error as Error).message
    }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' }
    });
  }
}

export async function POST(request: NextRequest): Promise<Response> {
    // ... 此函数代码无需修改，保持原样 ...
  try {
    const result = await syncInoreaderToNotion();
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: { 'Content-Type': 'application/json' }
    });
  } catch (error) {
    return new Response(JSON.stringify({
      success: false,
      message: (error as Error).message
    }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' }
    });
  }
}