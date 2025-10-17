import { Redis } from '@upstash/redis';
import { Client } from '@notionhq/client';
import { NextRequest } from 'next/server';
import type {
  BlockObjectRequest, // 仅保留导出的联合类型
  CreatePageParameters,
  DatabaseObjectResponse,
  PageObjectResponse,
  PartialPageObjectResponse,
  PartialDatabaseObjectResponse
} from '@notionhq/client/build/src/api-endpoints';

// 类型定义
interface TokenData {
  accessToken: string;
  refreshToken: string;
  expiresAt: number;
}

interface InoreaderItem {
  id: string;
  title?: string;
  canonical?: Array<{ href: string }>;
  alternate?: Array<{ href: string }>;
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

// 初始化Redis客户端（添加连接验证）
console.log(`[${new Date().toISOString()}] [初始化] 开始初始化Redis客户端`);
if (!process.env.KV_REST_API_URL || !process.env.KV_REST_API_TOKEN) {
  const errorMsg = 'Redis环境变量配置错误：KV_REST_API_URL或KV_REST_API_TOKEN缺失';
  console.error(`[${new Date().toISOString()}] [初始化] 致命错误: ${errorMsg}`);
  throw new Error(errorMsg);
}

const redis = new Redis({
  url: process.env.KV_REST_API_URL,
  token: process.env.KV_REST_API_TOKEN,
});

// 验证Redis连接
try {
  await redis.ping();
  console.log(`[${new Date().toISOString()}] [初始化] Redis连接成功`);
} catch (pingError) {
  const errorMsg = `Redis连接失败: ${(pingError as Error).message}`;
  console.error(`[${new Date().toISOString()}] [初始化] 致命错误: ${errorMsg}`);
  throw new Error(errorMsg);
}

// 初始化Notion客户端
console.log(`[${new Date().toISOString()}] [初始化] 开始初始化Notion客户端`);
if (!process.env.NOTION_TOKEN) {
  const errorMsg = 'Notion环境变量配置错误：NOTION_TOKEN缺失';
  console.error(`[${new Date().toISOString()}] [初始化] 致命错误: ${errorMsg}`);
  throw new Error(errorMsg);
}

const notion = new Client({
  auth: process.env.NOTION_TOKEN,
});
console.log(`[${new Date().toISOString()}] [初始化] Notion客户端初始化完成`);

// 刷新Inoreader令牌
async function refreshInoreaderToken(refreshToken: string): Promise<string> {
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
      const errorMsg = `令牌刷新失败: ${response.status} ${response.statusText}，响应内容: ${errorBody.substring(0, 200)}`;
      console.error(`[${new Date().toISOString()}] [${functionId}] [refreshInoreaderToken] 错误: ${errorMsg}`);
      throw new Error(errorMsg);
    }
    
    const tokenData: any = await response.json();
    console.log(`[${new Date().toISOString()}] [${functionId}] 成功解析令牌数据，包含字段: ${Object.keys(tokenData).join(', ')}`);
    
    // 验证刷新令牌响应的关键字段
    if (typeof tokenData.access_token !== 'string') {
      throw new Error('Inoreader返回的令牌数据不包含有效的access_token');
    }
    if (typeof tokenData.refresh_token !== 'string' || tokenData.refresh_token.trim() === '') {
      throw new Error('Inoreader返回的令牌数据不包含有效的refresh_token，需重新授权');
    }
    if (typeof tokenData.expires_in !== 'number' || tokenData.expires_in <= 0) {
      throw new Error(`Inoreader返回的有效期无效: ${tokenData.expires_in}`);
    }
    
    // 计算过期时间
    const expiresAt = Date.now() + (tokenData.expires_in * 1000);
    console.log(`[${new Date().toISOString()}] [${functionId}] 计算令牌过期时间: ${new Date(expiresAt).toISOString()}`);
    
    // 保存新的令牌数据
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
  const startTime = Date.now();
  const syncId = `sync_${Date.now().toString().slice(-6)}`;
  console.log(`[${new Date().toISOString()}] [${syncId}] 开始执行同步任务`);
  
  try {
    // 1. 从Redis获取Inoreader令牌（强化日志和解析）
    console.log(`[${new Date().toISOString()}] [${syncId}] 步骤1/3: 获取Inoreader令牌`);
    let tokenDataStr: string | null = await redis.get('inoreader_tokens'); // 显式声明类型
    
    // 详细日志：打印读取到的原始数据状态（空值兜底 + 类型安全）
    const logData = tokenDataStr 
      ? `长度=${tokenDataStr.length}，前10字符=${tokenDataStr.substring(0, 10)}...` 
      : '值为 null';
    console.log(`[${new Date().toISOString()}] [${syncId}] 从Redis读取令牌数据: ${logData}`);
    
    if (!tokenDataStr || typeof tokenDataStr !== 'string') {
      throw new Error('未找到有效的Inoreader令牌数据，请先完成授权');
    }
    
    // 强化令牌解析逻辑：验证必需字段
    let tokenData: TokenData;
    try {
      const parsed = JSON.parse(tokenDataStr);
      const requiredFields: Array<{name: keyof TokenData, type: 'string' | 'number'}> = [
        { name: 'accessToken', type: 'string' },
        { name: 'refreshToken', type: 'string' },
        { name: 'expiresAt', type: 'number' }
      ];
      
      // 检查每个必需字段
      for (const field of requiredFields) {
        if (!(field.name in parsed)) {
          throw new Error(`缺少必需字段: ${field.name}`);
        }
        if (typeof parsed[field.name] !== field.type) {
          throw new Error(`字段${field.name}类型错误，期望${field.type}，实际${typeof parsed[field.name]}`);
        }
      }
      
      // 仅提取必需字段，忽略额外字段
      tokenData = {
        accessToken: parsed.accessToken,
        refreshToken: parsed.refreshToken,
        expiresAt: parsed.expiresAt
      };
      console.log(`[${new Date().toISOString()}] [${syncId}] 令牌数据解析成功，有效期至: ${new Date(tokenData.expiresAt).toISOString()}`);
    } catch (parseError) {
      const errorInstance = parseError as Error;
      throw new Error(`解析令牌数据失败: ${errorInstance.message}（原始数据前50字符: ${tokenDataStr.substring(0, 50)}...）`);
    }
    
    // 检查令牌是否过期
    let { accessToken, refreshToken } = tokenData;
    const now = Date.now();
    const isExpired = now > tokenData.expiresAt;
    console.log(`[${new Date().toISOString()}] [${syncId}] 令牌状态: ${isExpired ? '已过期' : '未过期'}，当前时间: ${new Date(now).toISOString()}`);
    
    if (isExpired) {
      console.log(`[${new Date().toISOString()}] [${syncId}] 开始刷新令牌`);
      accessToken = await refreshInoreaderToken(refreshToken);
      
      // 刷新后重新获取令牌数据（显式断言为string）
      const updatedTokenDataStr = await redis.get('inoreader_tokens');
      if (updatedTokenDataStr) {
        tokenData = JSON.parse(updatedTokenDataStr as string) as TokenData;
        console.log(`[${new Date().toISOString()}] [${syncId}] 令牌已刷新，新有效期至: ${new Date(tokenData.expiresAt).toISOString()}`);
      }
    }

    // 2. 从Inoreader拉取星标文章
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
          throw new Error(`刷新令牌后仍失败: ${starredItemsResponse.status}，内容: ${errorBody.substring(0, 200)}`);
        }
      } else {
        const errorBody = await starredItemsResponse.text().catch(() => '无内容');
        throw new Error(`拉取文章失败: ${starredItemsResponse.status}，内容: ${errorBody.substring(0, 200)}`);
      }
    }

    const starredData: InoreaderResponse = await starredItemsResponse.json();
    const items = Array.isArray(starredData.items) ? starredData.items : [];
    console.log(`[${new Date().toISOString()}] [${syncId}] 拉取到${items.length}篇星标文章`);
    
    // 限制导入数量
    const MAX_IMPORT = 10;
    const itemsToImport = items.slice(0, MAX_IMPORT);
    console.log(`[${new Date().toISOString()}] [${syncId}] 准备导入前${itemsToImport.length}篇文章`);

    // 3. 同步到Notion数据库
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
      
      // 获取文章URL
      const articleUrl = 
        (Array.isArray(item.canonical) && item.canonical[0]?.href) || 
        (Array.isArray(item.alternate) && item.alternate[0]?.href);
      
      if (!articleUrl || typeof articleUrl !== 'string') {
        console.log(`[${new Date().toISOString()}] [${syncId}] 文章[${itemId}]无有效URL，跳过`);
        skippedCount++;
        continue;
      }

      // 检查Notion中是否已存在
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

      // 提取文章内容和元数据
      const content = item.summary?.content || '';
      const imageUrl = Array.isArray(item.enclosure) ? item.enclosure[0]?.href || '' : '';
      
      // 处理发布时间
      let publishedDate = new Date().toISOString();
      if (item.published) {
        const timestamp = typeof item.published === 'string' 
          ? parseInt(item.published, 10) 
          : item.published;
        if (!isNaN(timestamp) && timestamp > 0) {
          publishedDate = new Date(timestamp * 1000).toISOString();
        }
      }

      // 创建Notion页面（改用BlockObjectRequest联合类型）
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
            // 图片块：断言为BlockObjectRequest
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
            // 段落块：断言为BlockObjectRequest
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

        // 显式类型断言解决属性访问错误
        const nameProp = pageData.properties.Name as { title: Array<{ text: { content: string } }> };
        const logTitle = nameProp.title[0]?.text?.content?.substring(0, 30) || '无标题';
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
    throw new Error(`同步失败: ${errorInstance.message}`);
  }
}

// GET方法处理
export async function GET(request: NextRequest): Promise<Response> {
  const requestId = `get_${Date.now().toString().slice(-6)}`;
  console.log(`[${new Date().toISOString()}] [${requestId}] 收到GET请求`);
  try {
    const result = await syncInoreaderToNotion();
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: { 'Content-Type': 'application/json' }
    });
  } catch (error) {
    const errorMsg = (error as Error).message;
    return new Response(JSON.stringify({
      success: false,
      message: errorMsg,
      requestId: requestId
    }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' }
    });
  }
}

// POST方法处理
export async function POST(request: NextRequest): Promise<Response> {
  const requestId = `post_${Date.now().toString().slice(-6)}`;
  console.log(`[${new Date().toISOString()}] [${requestId}] 收到POST请求`);
  try {
    const result = await syncInoreaderToNotion();
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: { 'Content-Type': 'application/json' }
    });
  } catch (error) {
    const errorMsg = (error as Error).message;
    return new Response(JSON.stringify({
      success: false,
      message: errorMsg,
      requestId: requestId
    }), {
      status: 500,
      headers: { 'Content-Type': 'application/json' }
    });
  }
}