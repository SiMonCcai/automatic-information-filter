// /app/api/pull/route.ts

import { Redis } from '@upstash/redis';
import { Client } from '@notionhq/client';
import { NextRequest } from 'next/server';
import type {
  BlockObjectRequest,
  CreatePageParameters,
  DatabaseObjectResponse,
  PageObjectResponse,
  PartialPageObjectResponse,
  PartialDatabaseObjectResponse
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
  enclosure?: Array<{ href: string }>;
  published?: number | string;
  origin?: { title: string };
}

interface InoreaderResponse {
  items?: InoreaderItem[];
  continuation?: string;
  [key: string]: any;
}

interface SyncResult {
  success: boolean;
  message: string;
  imported: number;
  skipped: number;
  totalProcessed: number;
  duration: number;
  syncId: string;
}

// --- 客户端初始化 (移除顶层 await) ---
// 在顶层创建客户端实例，这是一个轻量级的同步操作
// 实际的网络连接会在第一次调用命令时建立
const redis = new Redis({
  url: process.env.KV_REST_API_URL!,
  token: process.env.KV_REST_API_TOKEN!,
});

const notion = new Client({
  auth: process.env.NOTION_TOKEN,
});


// --- 核心函数 (保持不变, 但现在会使用正确初始化的客户端) ---

// 刷新Inoreader令牌
async function refreshInoreaderToken(refreshToken: string): Promise<string> {
    // ... 这里的代码是正确的，无需修改 ...
    // 为了简洁，省略粘贴，请保留你原来的函数内容
  if (typeof refreshToken !== 'string' || refreshToken.trim() === '') {
    const errorMsg = '无效的刷新令牌: 必须提供非空字符串';
    console.error(`[${new Date().toISOString()}] [refreshInoreaderToken] 参数错误: ${errorMsg}`);
    throw new Error(errorMsg);
  }

  const startTime = Date.now();
  const functionId = `refresh_${Date.now().toString().slice(-6)}`;
  console.log(`[${new Date().toISOString()}] [${functionId}] [refreshInoreaderToken] 开始执行令牌刷新`);
  
  try {
    console.log(`[${new Date().toISOString()}] [${functionId}] 检查环境变量配置`);
    const clientId = process.env.INOREADER_CLIENT_ID;
    const clientSecret = process.env.INOREADER_CLIENT_SECRET;
    
    if (typeof clientId !== 'string' || clientId.trim() === '') {
      const errorMsg = '未配置有效的Inoreader客户端ID';
      console.error(`[${new Date().toISOString()}] [${functionId}] [refreshInoreaderToken] 错误: ${errorMsg}`);
      throw new Error(errorMsg);
    }
    
    if (typeof clientSecret !== 'string' || clientSecret.trim() === '') {
      const errorMsg = '未配置有效的Inoreader客户端密钥';
      console.error(`[${new Date().toISOString()}] [${functionId}] [refreshInoreaderToken] 错误: ${errorMsg}`);
      throw new Error(errorMsg);
    }
    
    console.log(`[${new Date().toISOString()}] [${functionId}] 环境变量配置检查通过`);
    
    console.log(`[${new Date().toISOString()}] [${functionId}] 准备向Inoreader发送令牌刷新请求`);
    const response = await fetch('https://www.inoreader.com/oauth2/token', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
      },
      body: new URLSearchParams({
        client_id: clientId,
        client_secret: clientSecret,
        grant_type: 'refresh_token',
        refresh_token: refreshToken
      })
    });
    
    console.log(`[${new Date().toISOString()}] [${functionId}] 收到Inoreader响应，状态码: ${response.status}`);
    
    if (!response.ok) {
      const errorBody = await response.text().catch(() => '无法获取错误详情');
      const safeErrorBody = typeof errorBody === 'string' ? errorBody.substring(0, 200) : '无效响应内容';
      const errorMsg = `令牌刷新失败: ${response.status} ${response.statusText}，响应内容: ${safeErrorBody}`;
      console.error(`[${new Date().toISOString()}] [${functionId}] [refreshInoreaderToken] 错误: ${errorMsg}`);
      throw new Error(errorMsg);
    }
    
    const tokenData: any = await response.json();
    console.log(`[${new Date().toISOString()}] [${functionId}] 成功解析令牌数据，包含字段: ${Object.keys(tokenData).join(', ')}`);
    
    if (typeof tokenData.access_token !== 'string') {
      throw new Error('Inoreader返回的令牌数据不包含有效的access_token');
    }
    if (typeof tokenData.refresh_token !== 'string' || tokenData.refresh_token.trim() === '') {
      throw new Error('Inoreader返回的令牌数据不包含有效的refresh_token，需重新授权');
    }
    if (typeof tokenData.expires_in !== 'number' || tokenData.expires_in <= 0) {
      throw new Error(`Inoreader返回的有效期无效: ${tokenData.expires_in}`);
    }
    
    const expiresAt = Date.now() + (tokenData.expires_in * 1000);
    console.log(`[${new Date().toISOString()}] [${functionId}] 计算令牌过期时间: ${new Date(expiresAt).toISOString()}`);
    
    const newTokenData: TokenData = {
      accessToken: tokenData.access_token,
      refreshToken: tokenData.refresh_token,
      expiresAt: expiresAt
    };
    await redis.set('inoreader_tokens', JSON.stringify(newTokenData));
    console.log(`[${new Date().toISOString()}] [${functionId}] 新令牌数据已保存到Redis`);
    
    const duration = Date.now() - startTime;
    console.log(`[${new Date().toISOString()}] [${functionId}] 令牌刷新完成，耗时${duration}ms`);
    
    return newTokenData.accessToken;
  } catch (error) {
    const duration = Date.now() - startTime;
    const errorInstance = error as Error;
    console.error(`[${new Date().toISOString()}] [${functionId}] 令牌刷新失败，耗时${duration}ms: ${errorInstance.message}`);
    throw error;
  }
}

// 处理Inoreader和Notion同步的核心函数
async function syncInoreaderToNotion(): Promise<SyncResult> {
    // ... 这里的代码是正确的，无需修改 ...
    // 为了简洁，省略粘贴，请保留你原来的函数内容
  const startTime = Date.now();
  const syncId = `sync_${Date.now().toString().slice(-6)}`;
  console.log(`[${new Date().toISOString()}] [${syncId}] 开始执行同步任务`);
  
  try {
    console.log(`[${new Date().toISOString()}] [${syncId}] 步骤1/3: 获取Inoreader令牌`);
    let tokenDataStr: string | null = await redis.get('inoreader_tokens');
    
    let logData: string;
    if (tokenDataStr) {
      if (typeof tokenDataStr === 'string') {
        logData = `长度=${tokenDataStr.length}，前10字符=${tokenDataStr.substring(0, 10)}...`;
      } else {
        logData = `数据类型错误，实际类型: ${typeof tokenDataStr}`;
        console.error(`[${new Date().toISOString()}] [${syncId}] 令牌数据类型错误，非字符串`);
      }
    } else {
      logData = '值为 null';
    }
    console.log(`[${new Date().toISOString()}] [${syncId}] 从Redis读取令牌数据: ${logData}`);
    
    if (!tokenDataStr || typeof tokenDataStr !== 'string') {
      throw new Error('未找到有效的Inoreader令牌数据，请先完成授权');
    }
    
    let tokenData: TokenData;
    try {
      const parsed = JSON.parse(tokenDataStr);
      const requiredFields: Array<{name: keyof TokenData, type: 'string' | 'number'}> = [
        { name: 'accessToken', type: 'string' },
        { name: 'refreshToken', type: 'string' },
        { name: 'expiresAt', type: 'number' }
      ];
      
      for (const field of requiredFields) {
        if (!(field.name in parsed)) {
          throw new Error(`缺少必需字段: ${field.name}`);
        }
        if (typeof parsed[field.name] !== field.type) {
          throw new Error(`字段${field.name}类型错误，期望${field.type}，实际${typeof parsed[field.name]}`);
        }
      }
      
      tokenData = {
        accessToken: parsed.accessToken,
        refreshToken: parsed.refreshToken,
        expiresAt: parsed.expiresAt
      };
      console.log(`[${new Date().toISOString()}] [${syncId}] 令牌数据解析成功，有效期至: ${new Date(tokenData.expiresAt).toISOString()}`);
    } catch (parseError) {
      const errorInstance = parseError as Error;
      const safeTokenData = typeof tokenDataStr === 'string' ? tokenDataStr.substring(0, 50) : '无效字符串';
      throw new Error(`解析令牌数据失败: ${errorInstance.message}（原始数据前50字符: ${safeTokenData}...）`);
    }
    
    let { accessToken, refreshToken } = tokenData;
    const now = Date.now();
    const isExpired = now > tokenData.expiresAt;
    console.log(`[${new Date().toISOString()}] [${syncId}] 令牌状态: ${isExpired ? '已过期' : '未过期'}，当前时间: ${new Date(now).toISOString()}`);
    
    if (isExpired) {
      console.log(`[${new Date().toISOString()}] [${syncId}] 开始刷新令牌`);
      accessToken = await refreshInoreaderToken(refreshToken);
      
      const updatedTokenDataStr = await redis.get('inoreader_tokens');
      if (updatedTokenDataStr) {
        tokenData = JSON.parse(updatedTokenDataStr as string) as TokenData;
        console.log(`[${new Date().toISOString()}] [${syncId}] 令牌已刷新，新有效期至: ${new Date(tokenData.expiresAt).toISOString()}`);
      }
    }

    console.log(`[${new Date().toISOString()}] [${syncId}] 步骤2/3: 拉取Inoreader星标文章`);
    let starredItemsResponse: Response;
    try {
      starredItemsResponse = await fetch(
        'https://www.inoreader.com/reader/api/0/stream/contents/user/-/state/com.google/starred',
        { headers: { 'Authorization': `Bearer ${accessToken}` } }
      );
      console.log(`[${new Date().toISOString()}] [${syncId}] 拉取文章响应状态: ${starredItemsResponse.status}`);
    } catch (fetchError) {
      throw new Error(`拉取星标文章失败: ${(fetchError as Error).message}`);
    }

    if (!starredItemsResponse.ok) {
      if (starredItemsResponse.status === 401 && refreshToken) {
        console.log(`[${new Date().toISOString()}] [${syncId}] 令牌无效，尝试刷新后重试`);
        accessToken = await refreshInoreaderToken(refreshToken);
        
        starredItemsResponse = await fetch(
          'https://www.inoreader.com/reader/api/0/stream/contents/user/-/state/com.google/starred',
          { headers: { 'Authorization': `Bearer ${accessToken}` } }
        );
        
        if (!starredItemsResponse.ok) {
          const errorBody = await starredItemsResponse.text().catch(() => '无内容');
          const safeErrorBody = typeof errorBody === 'string' ? errorBody.substring(0, 200) : '无效响应内容';
          throw new Error(`刷新令牌后仍失败: ${starredItemsResponse.status}，内容: ${safeErrorBody}`);
        }
      } else {
        const errorBody = await starredItemsResponse.text().catch(() => '无内容');
        const safeErrorBody = typeof errorBody === 'string' ? errorBody.substring(0, 200) : '无效响应内容';
        throw new Error(`拉取文章失败: ${starredItemsResponse.status}，内容: ${safeErrorBody}`);
      }
    }

    const starredData: InoreaderResponse = await starredItemsResponse.json();
    const items = Array.isArray(starredData.items) ? starredData.items : [];
    console.log(`[${new Date().toISOString()}] [${syncId}] 拉取到${items.length}篇星标文章`);
    
    const MAX_IMPORT = 10;
    const itemsToImport = items.slice(0, MAX_IMPORT);
    console.log(`[${new Date().toISOString()}] [${syncId}] 准备导入前${itemsToImport.length}篇文章`);

    console.log(`[${new Date().toISOString()}] [${syncId}] 步骤3/3: 同步到Notion数据库`);
    const databaseId = process.env.NOTION_DATABASE_ID;
    if (!databaseId || typeof databaseId !== 'string') {
      throw new Error('未配置有效的Notion数据库ID');
    }

    let importedCount = 0;
    let skippedCount = 0;

    for (const item of itemsToImport) {
      const itemId = item.id || '未知ID';
      console.log(`[${new Date().toISOString()}] [${syncId}] 处理文章 [ID: ${itemId}]`);
      
      const articleUrl = 
        (Array.isArray(item.canonical) && item.canonical[0]?.href) || 
        (Array.isArray(item.alternate) && item.alternate[0]?.href);
      
      if (!articleUrl || typeof articleUrl !== 'string') {
        console.log(`[${new Date().toISOString()}] [${syncId}] 文章[${itemId}]无有效URL，跳过`);
        skippedCount++;
        continue;
      }

      let existingPages: {
        results: (PageObjectResponse | DatabaseObjectResponse | PartialPageObjectResponse | PartialDatabaseObjectResponse)[];
      };
      
      try {
        existingPages = await notion.databases.query({
          database_id: databaseId,
          filter: { property: 'URL', url: { equals: articleUrl } }
        });
      } catch (queryError) {
        console.error(`[${new Date().toISOString()}] [${syncId}] 检查文章存在性失败: ${(queryError as Error).message}`);
        skippedCount++;
        continue;
      }

      if (existingPages.results.length > 0) {
        console.log(`[${new Date().toISOString()}] [${syncId}] 文章[${itemId}]已存在，跳过`);
        skippedCount++;
        continue;
      }

      const content = item.summary?.content || '';
      const imageUrl = Array.isArray(item.enclosure) ? item.enclosure[0]?.href || '' : '';
      
      let publishedDate = new Date().toISOString();
      if (item.published) {
        const timestamp = typeof item.published === 'string' 
          ? parseInt(item.published, 10) 
          : item.published;
        if (!isNaN(timestamp) && timestamp > 0) {
          publishedDate = new Date(timestamp * 1000).toISOString();
        }
      }

      try {
        const pageTitle = item.title?.trim() || '无标题文章';
        const sourceName = item.origin?.title?.trim() || '未知来源';

        const pageData: CreatePageParameters = {
          parent: { database_id: databaseId },
          properties: {
            Name: { title: [{ type: 'text', text: { content: pageTitle } }] },
            URL: { url: articleUrl },
            '来源': { select: { name: sourceName } },
            '收藏时间': { date: { start: publishedDate } }
          },
          children: [
            ...(imageUrl ? [
              {
                object: 'block',
                type: 'image',
                image: {
                  type: 'external',
                  external: { url: imageUrl }
                }
              } as BlockObjectRequest
            ] : []),
            {
              object: 'block',
              type: 'paragraph',
              paragraph: {
                rich_text: [
                  {
                    type: 'text',
                    text: { content: content || '无摘要' }
                  }
                ]
              }
            } as BlockObjectRequest
          ]
        };

        const nameProp = pageData.properties.Name as { title: Array<{ text: { content: string } }> };
        let logTitle: string;
        const rawTitle = nameProp.title[0]?.text?.content;
        if (typeof rawTitle === 'string') {
          logTitle = rawTitle.substring(0, 30) || '无标题';
        } else {
          logTitle = '无标题（标题非字符串）';
          console.warn(`[${new Date().toISOString()}] [${syncId}] 文章标题非字符串类型: ${typeof rawTitle}`);
        }

        const sourceProp = pageData.properties['来源'] as { select: { name: string } };
        const logSource = sourceProp.select.name || '未知来源';
        console.log(`[${new Date().toISOString()}] [${syncId}] 创建页面: 标题=${logTitle}..., 来源=${logSource}`);

        await notion.pages.create(pageData);
        console.log(`[${new Date().toISOString()}] [${syncId}] 文章[${itemId}]导入成功`);
        importedCount++;
      } catch (createError) {
        console.error(`[${new Date().toISOString()}] [${syncId}] 创建页面失败: ${(createError as Error).message}`);
        skippedCount++;
      }
    }

    const duration = Date.now() - startTime;
    console.log(`[${new Date().toISOString()}] [${syncId}] 同步完成，耗时${duration}ms - 成功: ${importedCount}, 跳过: ${skippedCount}`);
    
    return {
      success: true,
      message: `同步完成，成功导入${importedCount}篇，跳过${skippedCount}篇`,
      imported: importedCount,
      skipped: skippedCount,
      totalProcessed: itemsToImport.length,
      duration: duration,
      syncId: syncId
    };
  } catch (error) {
    const duration = Date.now() - startTime;
    const errorInstance = error as Error;
    console.error(`[${new Date().toISOString()}] [${syncId}] 同步失败，耗时${duration}ms: ${errorInstance.message}`);
    // 关键修改：向上抛出原始错误，而不是一个新的错误实例
    // 这样，调用方可以获得更完整的堆栈信息
    throw error; 
  }
}

// --- 请求处理 (保持不变) ---
export async function GET(request: NextRequest): Promise<Response> {
  const requestId = `get_${Date.now().toString().slice(-6)}`;
  console.log(`[${new Date().toISOString()}] [${requestId}] 收到GET请求, 开始执行同步...`);
  try {
    // 检查并确保关键环境变量存在
    if (!process.env.KV_REST_API_URL || !process.env.KV_REST_API_TOKEN || !process.env.NOTION_TOKEN) {
        throw new Error("关键环境变量 (KV_REST_API_URL, KV_REST_API_TOKEN, NOTION_TOKEN) 缺失!");
    }
    const result = await syncInoreaderToNotion();
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: { 'Content-Type': 'application/json' }
    });
  } catch (error) {
    const errorObj = error as Error;
    const errorMsg = errorObj.message;
    const errorStack = errorObj.stack || '无堆栈信息';
    console.error(`[${new Date().toISOString()}] [${requestId}] GET请求处理失败: ${errorMsg}\n${errorStack}`);
    return new Response(JSON.stringify({
      success: false,
      message: `GET请求失败: ${errorMsg}`,
      stack: errorStack,
      requestId: requestId
    }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' }
    });
  }
}

export async function POST(request: NextRequest): Promise<Response> {
  const requestId = `post_${Date.now().toString().slice(-6)}`;
  console.log(`[${new Date().toISOString()}] [${requestId}] 收到POST请求, 开始执行同步...`);
  try {
    // 检查并确保关键环境变量存在
    if (!process.env.KV_REST_API_URL || !process.env.KV_REST_API_TOKEN || !process.env.NOTION_TOKEN) {
        throw new Error("关键环境变量 (KV_REST_API_URL, KV_REST_API_TOKEN, NOTION_TOKEN) 缺失!");
    }
    const result = await syncInoreaderToNotion();
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: { 'Content-Type': 'application/json' }
    });
  } catch (error) {
    const errorObj = error as Error;
    const errorMsg = errorObj.message;
    const errorStack = errorObj.stack || '无堆栈信息';
    console.error(`[${new Date().toISOString()}] [${requestId}] POST请求处理失败: ${errorMsg}\n${errorStack}`);
    return new Response(JSON.stringify({
      success: false,
      message: `POST请求失败: ${errorMsg}`,
      stack: errorStack,
      requestId: requestId
    }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' }
    });
  }
}