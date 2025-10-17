import { Redis } from '@upstash/redis';
import { Client } from '@notionhq/client';

// 初始化Redis客户端
const redis = new Redis({
  url: process.env.KV_REST_API_URL,
  token: process.env.KV_REST_API_TOKEN,
});

// 初始化Notion客户端
const notion = new Client({
  auth: process.env.NOTION_TOKEN,
});

// 刷新Inoreader令牌
async function refreshInoreaderToken(refreshToken) {
  const startTime = Date.now();
  console.log(`[${new Date().toISOString()}] [refreshInoreaderToken] 开始执行令牌刷新`);
  
  try {
    const clientId = process.env.INOREADER_CLIENT_ID;
    const clientSecret = process.env.INOREADER_CLIENT_SECRET;
    
    if (!clientId || !clientSecret) {
      const errorMsg = '未配置Inoreader客户端ID或密钥';
      console.error(`[${new Date().toISOString()}] [refreshInoreaderToken] 错误: ${errorMsg}`);
      throw new Error(errorMsg);
    }
    
    console.log(`[${new Date().toISOString()}] [refreshInoreaderToken] 向Inoreader发送令牌刷新请求`);
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
    
    if (!response.ok) {
      const errorMsg = `令牌刷新失败: ${response.status} ${response.statusText}`;
      console.error(`[${new Date().toISOString()}] [refreshInoreaderToken] 错误: ${errorMsg}`);
      throw new Error(errorMsg);
    }
    
    const tokenData = await response.json();
    console.log(`[${new Date().toISOString()}] [refreshInoreaderToken] 成功获取新令牌数据`);
    
    // 计算过期时间（当前时间 + 有效期秒数）
    const expiresAt = Date.now() + (tokenData.expires_in * 1000);
    
    // 保存新的令牌数据
    const newTokenData = {
      accessToken: tokenData.access_token,
      refreshToken: tokenData.refresh_token || refreshToken, // 如果返回新的refresh_token则使用，否则保留旧的
      expiresAt: expiresAt
    };
    
    console.log(`[${new Date().toISOString()}] [refreshInoreaderToken] 保存新令牌到Redis`);
    await redis.set('inoreader_tokens', JSON.stringify(newTokenData));
    
    const duration = Date.now() - startTime;
    console.log(`[${new Date().toISOString()}] [refreshInoreaderToken] 执行完成，耗时${duration}ms，新令牌有效期至: ${new Date(expiresAt).toISOString()}`);
    
    return newTokenData.accessToken;
  } catch (error) {
    const duration = Date.now() - startTime;
    console.error(`[${new Date().toISOString()}] [refreshInoreaderToken] 执行失败，耗时${duration}ms，错误:`, error.stack || error.message);
    throw error;
  }
}

// 处理Inoreader和Notion同步的核心函数
async function syncInoreaderToNotion() {
  const startTime = Date.now();
  console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 开始执行同步任务`);
  
  try {
    // 1. 从Redis获取Inoreader令牌
    console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 步骤1/3: 获取Inoreader令牌`);
    console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 尝试从Redis获取令牌数据`);
    const tokenDataStr = await redis.get('inoreader_tokens');
    
    if (!tokenDataStr) {
      const errorMsg = '未找到Inoreader令牌，请先完成授权';
      console.error(`[${new Date().toISOString()}] [syncInoreaderToNotion] 错误: ${errorMsg}`);
      throw new Error(errorMsg);
    }
    
    let tokenData = JSON.parse(tokenDataStr);
    let { accessToken, refreshToken } = tokenData;
    console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 成功获取令牌，当前令牌有效期至: ${new Date(tokenData.expiresAt).toISOString()}`);
    
    // 检查令牌是否过期，如果过期则尝试刷新
    if (Date.now() > tokenData.expiresAt) {
      console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 令牌已过期，需要刷新`);
      
      if (!refreshToken) {
        const errorMsg = '没有刷新令牌，无法刷新，请重新授权';
        console.error(`[${new Date().toISOString()}] [syncInoreaderToNotion] 错误: ${errorMsg}`);
        throw new Error(errorMsg);
      }
      
      // 尝试刷新令牌
      accessToken = await refreshInoreaderToken(refreshToken);
      
      // 重新获取更新后的令牌数据
      console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 获取更新后的令牌数据`);
      const updatedTokenDataStr = await redis.get('inoreader_tokens');
      if (updatedTokenDataStr) {
        tokenData = JSON.parse(updatedTokenDataStr);
        console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 已更新令牌数据，新有效期至: ${new Date(tokenData.expiresAt).toISOString()}`);
      } else {
        console.warn(`[${new Date().toISOString()}] [syncInoreaderToNotion] 刷新后未获取到新的令牌数据`);
      }
    }

    // 2. 从Inoreader拉取星标文章
    console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 步骤2/3: 拉取Inoreader星标文章`);
    let starredItemsResponse;
    try {
      starredItemsResponse = await fetch(
        'https://www.inoreader.com/reader/api/0/stream/contents/user/-/state/com.google/starred',
        {
          headers: {
            'Authorization': `Bearer ${accessToken}`,
          },
        }
      );
      console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 拉取星标文章响应状态: ${starredItemsResponse.status}`);
    } catch (fetchError) {
      const errorMsg = `拉取星标文章时网络错误: ${fetchError.message}`;
      console.error(`[${new Date().toISOString()}] [syncInoreaderToNotion] 错误: ${errorMsg}`);
      throw new Error(errorMsg);
    }

    if (!starredItemsResponse.ok) {
      // 如果是401错误，可能是令牌无效，尝试刷新后再试一次
      if (starredItemsResponse.status === 401 && refreshToken) {
        console.warn(`[${new Date().toISOString()}] [syncInoreaderToNotion] 令牌可能无效（401），尝试刷新后重试`);
        accessToken = await refreshInoreaderToken(refreshToken);
        
        // 重新尝试拉取数据
        console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 使用新令牌重新拉取星标文章`);
        starredItemsResponse = await fetch(
          'https://www.inoreader.com/reader/api/0/stream/contents/user/-/state/com.google/starred',
          {
            headers: {
              'Authorization': `Bearer ${accessToken}`,
            },
          }
        );
        
        if (!starredItemsResponse.ok) {
          const errorMsg = `刷新令牌后拉取Inoreader数据仍失败: ${starredItemsResponse.status} ${starredItemsResponse.statusText}`;
          console.error(`[${new Date().toISOString()}] [syncInoreaderToNotion] 错误: ${errorMsg}`);
          throw new Error(errorMsg);
        }
      } else {
        const errorMsg = `拉取Inoreader数据失败: ${starredItemsResponse.status} ${starredItemsResponse.statusText}`;
        console.error(`[${new Date().toISOString()}] [syncInoreaderToNotion] 错误: ${errorMsg}`);
        throw new Error(errorMsg);
      }
    }

    const starredData = await starredItemsResponse.json();
    const items = starredData.items || [];
    console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 成功拉取到${items.length}篇星标文章`);
    
    // 限制导入数量，避免一次性导入过多
    const MAX_IMPORT = 10;
    const itemsToImport = items.slice(0, MAX_IMPORT);
    console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 准备导入前${itemsToImport.length}篇文章（最大限制: ${MAX_IMPORT}）`);

    // 3. 同步到Notion数据库
    console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 步骤3/3: 同步到Notion数据库`);
    const databaseId = process.env.NOTION_DATABASE_ID;
    if (!databaseId) {
      const errorMsg = '未配置Notion数据库ID';
      console.error(`[${new Date().toISOString()}] [syncInoreaderToNotion] 错误: ${errorMsg}`);
      throw new Error(errorMsg);
    }
    console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 目标Notion数据库ID: ${databaseId.substring(0, 8)}...`);

    let importedCount = 0;
    let skippedCount = 0;

    for (const item of itemsToImport) {
      const itemId = item.id || '未知ID';
      console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 开始处理文章 [ID: ${itemId}]，标题: ${item.title || '无标题'}`);
      
      // 获取文章URL，增加容错处理
      const articleUrl = item.canonical?.[0]?.href || item.alternate?.[0]?.href;
      if (!articleUrl) {
        console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 文章[${itemId}]无有效URL，跳过处理`);
        skippedCount++;
        continue;
      }

      // 检查是否已存在相同URL的页面
      console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 检查文章[${itemId}]在Notion中是否已存在: ${articleUrl.substring(0, 50)}...`);
      let existingPages;
      try {
        existingPages = await notion.databases.query({
          database_id: databaseId,
          filter: {
            property: 'URL',
            url: {
              equals: articleUrl
            }
          }
        });
      } catch (queryError) {
        console.error(`[${new Date().toISOString()}] [syncInoreaderToNotion] 检查文章[${itemId}]是否存在时出错:`, queryError.stack || queryError.message);
        skippedCount++;
        continue;
      }

      if (existingPages.results.length > 0) {
        console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 文章[${itemId}]已存在于Notion中，跳过`);
        skippedCount++;
        continue;
      }

      // 提取文章内容
      let content = '';
      if (item.summary?.content) {
        content = item.summary.content;
        console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 提取到文章[${itemId}]摘要，长度: ${content.length}字符`);
      } else {
        console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 文章[${itemId}]无摘要内容`);
      }

      // 提取第一张图片URL
      let imageUrl = '';
      if (item.enclosure?.length) {
        imageUrl = item.enclosure[0].href;
        console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 提取到文章[${itemId}]图片URL: ${imageUrl.substring(0, 50)}...`);
      } else {
        console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 文章[${itemId}]无图片`);
      }

      // 处理时间戳，确保是数字类型并转换为正确的日期
      let publishedDate;
      if (item.published) {
        const publishedTimestamp = typeof item.published === 'string' 
          ? parseInt(item.published, 10) 
          : item.published;
        
        // 检查时间戳有效性
        if (isNaN(publishedTimestamp) || publishedTimestamp <= 0) {
          console.warn(`[${new Date().toISOString()}] [syncInoreaderToNotion] 文章[${itemId}]的时间戳无效: ${item.published}`);
          publishedDate = new Date().toISOString();
        } else {
          // Inoreader的时间戳是秒级，需要转换为毫秒
          publishedDate = new Date(publishedTimestamp * 1000).toISOString();
        }
      } else {
        console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 文章[${itemId}]无发布时间，使用当前时间`);
        publishedDate = new Date().toISOString();
      }
      console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 文章[${itemId}]发布时间: ${publishedDate}`);

      // 创建Notion页面
      console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 开始创建文章[${itemId}]的Notion页面`);
      try {
        await notion.pages.create({
          parent: { database_id: databaseId },
          properties: {
            Name: {
              title: [
                {
                  text: {
                    content: item.title || '无标题文章'
                  }
                }
              ]
            },
            URL: {
              url: articleUrl
            },
            '来源': {
              select: {
                name: item.origin?.title || '未知来源'
              }
            },
            '收藏时间': {
              date: {
                start: publishedDate
              }
            }
          },
          children: [
            // 添加图片块（如果有图片）
            ...(imageUrl
              ? [
                  {
                    type: 'image',
                    image: {
                      type: 'external',
                      external: {
                        url: imageUrl
                      }
                    }
                  }
                ]
              : []),
            // 添加内容块
            {
              type: 'paragraph',
              paragraph: {
                rich_text: [
                  {
                    type: 'text',
                    text: {
                      content: content || '无摘要内容'
                    }
                  }
                ]
              }
            }
          ]
        });

        console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 文章[${itemId}]成功导入Notion`);
        importedCount++;
      } catch (createError) {
        console.error(`[${new Date().toISOString()}] [syncInoreaderToNotion] 创建文章[${itemId}]的Notion页面失败:`, createError.stack || createError.message);
        skippedCount++;
      }
    }

    const duration = Date.now() - startTime;
    console.log(`[${new Date().toISOString()}] [syncInoreaderToNotion] 同步任务完成，总耗时${duration}ms - 成功导入: ${importedCount}篇, 跳过: ${skippedCount}篇`);
    return {
      success: true,
      message: `成功同步 ${importedCount} 篇文章到Notion，跳过 ${skippedCount} 篇`,
      imported: importedCount,
      skipped: skippedCount,
      totalProcessed: itemsToImport.length,
      duration: duration
    };
  } catch (error) {
    const duration = Date.now() - startTime;
    console.error(`[${new Date().toISOString()}] [syncInoreaderToNotion] 同步过程出错，总耗时${duration}ms:`, error.stack || error.message);
    throw new Error(`同步失败: ${error.message}`);
  }
}

// 支持GET方法
export async function GET(request) {
  const requestId = Date.now().toString();
  console.log(`[${new Date().toISOString()}] [GET] 收到请求 [ID: ${requestId}]`);
  try {
    const result = await syncInoreaderToNotion();
    console.log(`[${new Date().toISOString()}] [GET] 请求[${requestId}]处理成功`);
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: {
        'Content-Type': 'application/json'
      }
    });
  } catch (error) {
    console.error(`[${new Date().toISOString()}] [GET] 请求[${requestId}]处理失败:`, error.message);
    return new Response(JSON.stringify({
      success: false,
      message: error.message,
      requestId: requestId
    }), {
      status: 500,
      headers: {
        'Content-Type': 'application/json'
      }
    });
  }
}

// 处理POST方法
export async function POST(request) {
  const requestId = Date.now().toString();
  console.log(`[${new Date().toISOString()}] [POST] 收到请求 [ID: ${requestId}]`);
  try {
    const result = await syncInoreaderToNotion();
    console.log(`[${new Date().toISOString()}] [POST] 请求[${requestId}]处理成功`);
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: {
        'Content-Type': 'application/json'
      }
    });
  } catch (error) {
    console.error(`[${new Date().toISOString()}] [POST] 请求[${requestId}]处理失败:`, error.message);
    return new Response(JSON.stringify({
      success: false,
      message: error.message,
      requestId: requestId
    }), {
      status: 500,
      headers: {
        'Content-Type': 'application/json'
      }
    });
  }
}
