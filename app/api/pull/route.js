import { Redis } from '@upstash/redis';
import { Client } from '@notionhq/client';

// 初始化Redis客户端
console.log(`[${new Date().toISOString()}] [初始化] 开始初始化Redis客户端`);
const redis = new Redis({
  url: process.env.KV_REST_API_URL,
  token: process.env.KV_REST_API_TOKEN,
});
console.log(`[${new Date().toISOString()}] [初始化] Redis客户端初始化完成`);

// 初始化Notion客户端
console.log(`[${new Date().toISOString()}] [初始化] 开始初始化Notion客户端`);
const notion = new Client({
  auth: process.env.NOTION_TOKEN,
});
console.log(`[${new Date().toISOString()}] [初始化] Notion客户端初始化完成`);

// 刷新Inoreader令牌
async function refreshInoreaderToken(refreshToken) {
  const startTime = Date.now();
  const functionId = `refresh_${Date.now().toString().slice(-6)}`;
  console.log(`[${new Date().toISOString()}] [${functionId}] [refreshInoreaderToken] 开始执行令牌刷新`);
  
  try {
    console.log(`[${new Date().toISOString()}] [${functionId}] 检查环境变量配置`);
    const clientId = process.env.INOREADER_CLIENT_ID;
    const clientSecret = process.env.INOREADER_CLIENT_SECRET;
    
    if (!clientId || !clientSecret) {
      const errorMsg = '未配置Inoreader客户端ID或密钥';
      console.error(`[${new Date().toISOString()}] [${functionId}] [refreshInoreaderToken] 错误: ${errorMsg}`);
      throw new Error(errorMsg);
    }
    console.log(`[${new Date().toISOString()}] [${functionId}] 环境变量配置检查通过`);
    
    console.log(`[${new Date().toISOString()}] [${functionId}] 准备向Inoreader发送令牌刷新请求`);
    console.log(`[${new Date().toISOString()}] [${functionId}] 请求URL: https://www.inoreader.com/oauth2/token`);
    console.log(`[${new Date().toISOString()}] [${functionId}] 请求参数: client_id=${clientId.substring(0, 8)}..., grant_type=refresh_token, refresh_token=${refreshToken.substring(0, 8)}...`);
    
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
    
    const tokenData = await response.json();
    console.log(`[${new Date().toISOString()}] [${functionId}] 成功解析令牌数据，包含字段: ${Object.keys(tokenData).join(', ')}`);
    
    // 计算过期时间（当前时间 + 有效期秒数）
    const expiresAt = Date.now() + (tokenData.expires_in * 1000);
    console.log(`[${new Date().toISOString()}] [${functionId}] 计算令牌过期时间: 有效期${tokenData.expires_in}秒，过期时间${new Date(expiresAt).toISOString()}`);
    
    // 保存新的令牌数据
    const newTokenData = {
      accessToken: tokenData.access_token,
      refreshToken: tokenData.refresh_token || refreshToken,
      expiresAt: expiresAt
    };
    console.log(`[${new Date().toISOString()}] [${functionId}] 准备保存新令牌数据: accessToken=${newTokenData.accessToken.substring(0, 8)}..., refreshToken=${newTokenData.refreshToken.substring(0, 8)}...`);
    
    await redis.set('inoreader_tokens', JSON.stringify(newTokenData));
    console.log(`[${new Date().toISOString()}] [${functionId}] 新令牌数据已成功保存到Redis`);
    
    const duration = Date.now() - startTime;
    console.log(`[${new Date().toISOString()}] [${functionId}] [refreshInoreaderToken] 执行完成，耗时${duration}ms，新令牌有效期至: ${new Date(expiresAt).toISOString()}`);
    
    return newTokenData.accessToken;
  } catch (error) {
    const duration = Date.now() - startTime;
    console.error(`[${new Date().toISOString()}] [${functionId}] [refreshInoreaderToken] 执行失败，耗时${duration}ms，错误:`, error.stack || error.message);
    throw error;
  }
}

// 处理Inoreader和Notion同步的核心函数
async function syncInoreaderToNotion() {
  const startTime = Date.now();
  const syncId = `sync_${Date.now().toString().slice(-6)}`;
  console.log(`[${new Date().toISOString()}] [${syncId}] [syncInoreaderToNotion] 开始执行同步任务`);
  
  try {
    // 1. 从Redis获取Inoreader令牌
    console.log(`[${new Date().toISOString()}] [${syncId}] 步骤1/3: 获取Inoreader令牌`);
    console.log(`[${new Date().toISOString()}] [${syncId}] 尝试从Redis获取键为"inoreader_tokens"的数据`);
    
    const tokenDataStr = await redis.get('inoreader_tokens');
    console.log(`[${new Date().toISOString()}] [${syncId}] Redis查询完成，${tokenDataStr ? '已获取' : '未获取'}令牌数据`);
    
    if (!tokenDataStr) {
      const errorMsg = '未找到Inoreader令牌，请先完成授权';
      console.error(`[${new Date().toISOString()}] [${syncId}] 错误: ${errorMsg}`);
      throw new Error(errorMsg);
    }
    
    let tokenData = JSON.parse(tokenDataStr);
    let { accessToken, refreshToken } = tokenData;
    console.log(`[${new Date().toISOString()}] [${syncId}] 成功解析令牌数据，当前令牌有效期至: ${new Date(tokenData.expiresAt).toISOString()}`);
    console.log(`[${new Date().toISOString()}] [${syncId}] 访问令牌前8位: ${accessToken.substring(0, 8)}...`);
    console.log(`[${new Date().toISOString()}] [${syncId}] 刷新令牌前8位: ${refreshToken.substring(0, 8)}...`);
    
    // 检查令牌是否过期，如果过期则尝试刷新
    const now = Date.now();
    const isExpired = now > tokenData.expiresAt;
    console.log(`[${new Date().toISOString()}] [${syncId}] 令牌状态检查: 当前时间${new Date(now).toISOString()}, 过期时间${new Date(tokenData.expiresAt).toISOString()}, ${isExpired ? '已过期' : '未过期'}`);
    
    if (isExpired) {
      console.log(`[${new Date().toISOString()}] [${syncId}] 令牌已过期，需要刷新`);
      
      if (!refreshToken) {
        const errorMsg = '没有刷新令牌，无法刷新，请重新授权';
        console.error(`[${new Date().toISOString()}] [${syncId}] 错误: ${errorMsg}`);
        throw new Error(errorMsg);
      }
      
      // 尝试刷新令牌
      console.log(`[${new Date().toISOString()}] [${syncId}] 调用refreshInoreaderToken进行令牌刷新`);
      accessToken = await refreshInoreaderToken(refreshToken);
      
      // 重新获取更新后的令牌数据
      console.log(`[${new Date().toISOString()}] [${syncId}] 从Redis获取更新后的令牌数据`);
      const updatedTokenDataStr = await redis.get('inoreader_tokens');
      
      if (updatedTokenDataStr) {
        tokenData = JSON.parse(updatedTokenDataStr);
        console.log(`[${new Date().toISOString()}] [${syncId}] 已更新令牌数据，新有效期至: ${new Date(tokenData.expiresAt).toISOString()}`);
      } else {
        console.warn(`[${new Date().toISOString()}] [${syncId}] 刷新后未获取到新的令牌数据`);
      }
    }

    // 2. 从Inoreader拉取星标文章
    console.log(`[${new Date().toISOString()}] [${syncId}] 步骤2/3: 拉取Inoreader星标文章`);
    console.log(`[${new Date().toISOString()}] [${syncId}] 请求URL: https://www.inoreader.com/reader/api/0/stream/contents/user/-/state/com.google/starred`);
    
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
      console.log(`[${new Date().toISOString()}] [${syncId}] 拉取星标文章响应状态: ${starredItemsResponse.status}`);
    } catch (fetchError) {
      const errorMsg = `拉取星标文章时网络错误: ${fetchError.message}`;
      console.error(`[${new Date().toISOString()}] [${syncId}] 错误: ${errorMsg}`);
      throw new Error(errorMsg);
    }

    if (!starredItemsResponse.ok) {
      // 如果是401错误，可能是令牌无效，尝试刷新后再试一次
      if (starredItemsResponse.status === 401 && refreshToken) {
        console.warn(`[${new Date().toISOString()}] [${syncId}] 令牌可能无效（401），尝试刷新后重试`);
        accessToken = await refreshInoreaderToken(refreshToken);
        
        // 重新尝试拉取数据
        console.log(`[${new Date().toISOString()}] [${syncId}] 使用新令牌重新拉取星标文章`);
        starredItemsResponse = await fetch(
          'https://www.inoreader.com/reader/api/0/stream/contents/user/-/state/com.google/starred',
          {
            headers: {
              'Authorization': `Bearer ${accessToken}`,
            },
          }
        );
        
        console.log(`[${new Date().toISOString()}] [${syncId}] 第二次拉取星标文章响应状态: ${starredItemsResponse.status}`);
        
        if (!starredItemsResponse.ok) {
          const errorBody = await starredItemsResponse.text().catch(() => '无法获取错误详情');
          const errorMsg = `刷新令牌后拉取Inoreader数据仍失败: ${starredItemsResponse.status} ${starredItemsResponse.statusText}，响应内容: ${errorBody.substring(0, 200)}`;
          console.error(`[${new Date().toISOString()}] [${syncId}] 错误: ${errorMsg}`);
          throw new Error(errorMsg);
        }
      } else {
        const errorBody = await starredItemsResponse.text().catch(() => '无法获取错误详情');
        const errorMsg = `拉取Inoreader数据失败: ${starredItemsResponse.status} ${starredItemsResponse.statusText}，响应内容: ${errorBody.substring(0, 200)}`;
        console.error(`[${new Date().toISOString()}] [${syncId}] 错误: ${errorMsg}`);
        throw new Error(errorMsg);
      }
    }

    const starredData = await starredItemsResponse.json();
    console.log(`[${new Date().toISOString()}] [${syncId}] 成功解析星标文章数据，包含字段: ${Object.keys(starredData).join(', ')}`);
    
    const items = starredData.items || [];
    console.log(`[${new Date().toISOString()}] [${syncId}] 成功拉取到${items.length}篇星标文章`);
    
    // 限制导入数量，避免一次性导入过多
    const MAX_IMPORT = 10;
    const itemsToImport = items.slice(0, MAX_IMPORT);
    console.log(`[${new Date().toISOString()}] [${syncId}] 准备导入前${itemsToImport.length}篇文章（最大限制: ${MAX_IMPORT}）`);

    // 3. 同步到Notion数据库
    console.log(`[${new Date().toISOString()}] [${syncId}] 步骤3/3: 同步到Notion数据库`);
    const databaseId = process.env.NOTION_DATABASE_ID;
    
    if (!databaseId) {
      const errorMsg = '未配置Notion数据库ID';
      console.error(`[${new Date().toISOString()}] [${syncId}] 错误: ${errorMsg}`);
      throw new Error(errorMsg);
    }
    console.log(`[${new Date().toISOString()}] [${syncId}] 目标Notion数据库ID: ${databaseId.substring(0, 8)}...`);

    let importedCount = 0;
    let skippedCount = 0;

    for (const item of itemsToImport) {
      const itemId = item.id || '未知ID';
      console.log(`[${new Date().toISOString()}] [${syncId}] 开始处理文章 [ID: ${itemId}]，标题: ${item.title || '无标题'}`);
      
      // 获取文章URL，增加容错处理
      const articleUrl = item.canonical?.[0]?.href || item.alternate?.[0]?.href;
      console.log(`[${new Date().toISOString()}] [${syncId}] 解析文章URL: canonical=${item.canonical?.[0]?.href || '无'}, alternate=${item.alternate?.[0]?.href || '无'}, 最终使用=${articleUrl || '无'}`);
      
      if (!articleUrl) {
        console.log(`[${new Date().toISOString()}] [${syncId}] 文章[${itemId}]无有效URL，跳过处理`);
        skippedCount++;
        continue;
      }

      // 检查是否已存在相同URL的页面
      console.log(`[${new Date().toISOString()}] [${syncId}] 检查文章[${itemId}]在Notion中是否已存在: ${articleUrl.substring(0, 50)}...`);
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
        console.log(`[${new Date().toISOString()}] [${syncId}] Notion数据库查询完成，返回${existingPages.results.length}条结果`);
      } catch (queryError) {
        console.error(`[${new Date().toISOString()}] [${syncId}] 检查文章[${itemId}]是否存在时出错:`, queryError.stack || queryError.message);
        skippedCount++;
        continue;
      }

      if (existingPages.results.length > 0) {
        console.log(`[${new Date().toISOString()}] [${syncId}] 文章[${itemId}]已存在于Notion中（ID: ${existingPages.results[0].id.substring(0, 8)}...），跳过`);
        skippedCount++;
        continue;
      }

      // 提取文章内容
      let content = '';
      if (item.summary?.content) {
        content = item.summary.content;
        console.log(`[${new Date().toISOString()}] [${syncId}] 提取到文章[${itemId}]摘要，长度: ${content.length}字符，前50字符: ${content.substring(0, 50)}...`);
      } else {
        console.log(`[${new Date().toISOString()}] [${syncId}] 文章[${itemId}]无摘要内容`);
      }

      // 提取第一张图片URL
      let imageUrl = '';
      if (item.enclosure?.length) {
        imageUrl = item.enclosure[0].href;
        console.log(`[${new Date().toISOString()}] [${syncId}] 提取到文章[${itemId}]图片URL: ${imageUrl.substring(0, 50)}...`);
      } else {
        console.log(`[${new Date().toISOString()}] [${syncId}] 文章[${itemId}]无图片`);
      }

      // 处理时间戳，确保是数字类型并转换为正确的日期
      let publishedDate;
      if (item.published) {
        console.log(`[${new Date().toISOString()}] [${syncId}] 原始发布时间戳: ${item.published} (类型: ${typeof item.published})`);
        const publishedTimestamp = typeof item.published === 'string' 
          ? parseInt(item.published, 10) 
          : item.published;
        
        // 检查时间戳有效性
        if (isNaN(publishedTimestamp) || publishedTimestamp <= 0) {
          console.warn(`[${new Date().toISOString()}] [${syncId}] 文章[${itemId}]的时间戳无效: ${item.published}`);
          publishedDate = new Date().toISOString();
        } else {
          // Inoreader的时间戳是秒级，需要转换为毫秒
          publishedDate = new Date(publishedTimestamp * 1000).toISOString();
        }
      } else {
        console.log(`[${new Date().toISOString()}] [${syncId}] 文章[${itemId}]无发布时间，使用当前时间`);
        publishedDate = new Date().toISOString();
      }
      console.log(`[${new Date().toISOString()}] [${syncId}] 文章[${itemId}]最终发布时间: ${publishedDate}`);

      // 创建Notion页面
      console.log(`[${new Date().toISOString()}] [${syncId}] 开始创建文章[${itemId}]的Notion页面`);
      try {
        const pageData = {
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
        };
        
        console.log(`[${new Date().toISOString()}] [${syncId}] 准备创建页面数据: 标题=${pageData.properties.Name.title[0].text.content.substring(0, 30)}..., 来源=${pageData.properties.来源.select.name}`);
        const response = await notion.pages.create(pageData);
        
        console.log(`[${new Date().toISOString()}] [${syncId}] 文章[${itemId}]成功导入Notion，页面ID: ${response.id.substring(0, 8)}...`);
        importedCount++;
      } catch (createError) {
        console.error(`[${new Date().toISOString()}] [${syncId}] 创建文章[${itemId}]的Notion页面失败:`, createError.stack || createError.message);
        skippedCount++;
      }
    }

    const duration = Date.now() - startTime;
    console.log(`[${new Date().toISOString()}] [${syncId}] [syncInoreaderToNotion] 同步任务完成，总耗时${duration}ms - 成功导入: ${importedCount}篇, 跳过: ${skippedCount}篇`);
    return {
      success: true,
      message: `成功同步 ${importedCount} 篇文章到Notion，跳过 ${skippedCount} 篇`,
      imported: importedCount,
      skipped: skippedCount,
      totalProcessed: itemsToImport.length,
      duration: duration,
      syncId: syncId
    };
  } catch (error) {
    const duration = Date.now() - startTime;
    console.error(`[${new Date().toISOString()}] [${syncId}] [syncInoreaderToNotion] 同步过程出错，总耗时${duration}ms:`, error.stack || error.message);
    throw new Error(`同步失败: ${error.message}`);
  }
}

// 支持GET方法
export async function GET(request) {
  const requestId = `get_${Date.now().toString().slice(-6)}`;
  console.log(`[${new Date().toISOString()}] [${requestId}] [GET] 收到请求，URL: ${request.url}`);
  try {
    const result = await syncInoreaderToNotion();
    console.log(`[${new Date().toISOString()}] [${requestId}] [GET] 请求处理成功，结果: ${JSON.stringify({
      imported: result.imported,
      skipped: result.skipped,
      duration: result.duration
    })}`);
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: {
        'Content-Type': 'application/json'
      }
    });
  } catch (error) {
    console.error(`[${new Date().toISOString()}] [${requestId}] [GET] 请求处理失败:`, error.message);
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
  const requestId = `post_${Date.now().toString().slice(-6)}`;
  console.log(`[${new Date().toISOString()}] [${requestId}] [POST] 收到请求，URL: ${request.url}`);
  
  // 记录请求体（敏感信息会被截断）
  try {
    const requestBody = await request.clone().text();
    console.log(`[${new Date().toISOString()}] [${requestId}] 请求体前200字符: ${requestBody.substring(0, 200)}...`);
  } catch (e) {
    console.log(`[${new Date().toISOString()}] [${requestId}] 无法读取请求体: ${e.message}`);
  }
  
  try {
    const result = await syncInoreaderToNotion();
    console.log(`[${new Date().toISOString()}] [${requestId}] [POST] 请求处理成功，结果: ${JSON.stringify({
      imported: result.imported,
      skipped: result.skipped,
      duration: result.duration
    })}`);
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: {
        'Content-Type': 'application/json'
      }
    });
  } catch (error) {
    console.error(`[${new Date().toISOString()}] [${requestId}] [POST] 请求处理失败:`, error.message);
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
