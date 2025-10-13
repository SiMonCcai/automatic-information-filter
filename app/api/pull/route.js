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

// 处理Inoreader和Notion同步的核心函数
async function syncInoreaderToNotion() {
  console.log(`[${new Date().toISOString()}] 开始执行Inoreader到Notion的同步任务`);
  
  try {
    // 1. 从Redis获取Inoreader令牌
    console.log(`[${new Date().toISOString()}] 尝试从Redis获取Inoreader令牌`);
    const tokenDataStr = await redis.get('inoreader_tokens');
    
    if (!tokenDataStr) {
      console.error(`[${new Date().toISOString()}] 未找到Inoreader令牌`);
      throw new Error('未找到Inoreader令牌，请先完成授权');
    }
    
    const tokenData = JSON.parse(tokenDataStr);
    const { accessToken } = tokenData;
    console.log(`[${new Date().toISOString()}] 成功获取Inoreader令牌`);
    
    // 检查令牌是否过期
    if (Date.now() > tokenData.expiresAt) {
      console.error(`[${new Date().toISOString()}] Inoreader令牌已过期，过期时间: ${new Date(tokenData.expiresAt).toISOString()}`);
      throw new Error('Inoreader令牌已过期，请重新授权');
    }

    // 2. 从Inoreader拉取星标文章
    console.log(`[${new Date().toISOString()}] 开始从Inoreader拉取星标文章`);
    const starredItemsResponse = await fetch(
      'https://www.inoreader.com/reader/api/0/stream/contents/user/-/state/com.google/starred',
      {
        headers: {
          'Authorization': `Bearer ${accessToken}`,
        },
      }
    );

    if (!starredItemsResponse.ok) {
      console.error(`[${new Date().toISOString()}] 拉取Inoreader数据失败: ${starredItemsResponse.status} ${starredItemsResponse.statusText}`);
      throw new Error(`拉取Inoreader数据失败: ${starredItemsResponse.status} ${starredItemsResponse.statusText}`);
    }

    const starredData = await starredItemsResponse.json();
    const items = starredData.items || [];
    console.log(`[${new Date().toISOString()}] 成功拉取到${items.length}篇星标文章`);
    
    // 限制导入数量，避免一次性导入过多
    const MAX_IMPORT = 10;
    const itemsToImport = items.slice(0, MAX_IMPORT);
    console.log(`[${new Date().toISOString()}] 将导入前${itemsToImport.length}篇文章（最大限制: ${MAX_IMPORT}）`);

    // 3. 同步到Notion数据库
    const databaseId = process.env.NOTION_DATABASE_ID;
    if (!databaseId) {
      console.error(`[${new Date().toISOString()}] 未配置Notion数据库ID`);
      throw new Error('未配置Notion数据库ID');
    }
    console.log(`[${new Date().toISOString()}] 准备同步到Notion数据库，数据库ID: ${databaseId.substring(0, 8)}...`); // 只显示部分ID，保护敏感信息

    let importedCount = 0;
    let skippedCount = 0;

    for (const item of itemsToImport) {
      console.log(`[${new Date().toISOString()}] 处理文章: ${item.title || '无标题'}`);
      
      // 获取文章URL，增加容错处理
      const articleUrl = item.canonical?.[0]?.href || item.alternate?.[0]?.href;
      if (!articleUrl) {
        console.log(`[${new Date().toISOString()}] 文章URL不存在，跳过: ${item.title || '无标题'}`);
        skippedCount++;
        continue;
      }

      // 检查是否已存在相同URL的页面
      console.log(`[${new Date().toISOString()}] 检查Notion中是否已存在URL: ${articleUrl.substring(0, 50)}...`);
      const existingPages = await notion.databases.query({
        database_id: databaseId,
        filter: {
          property: 'URL',
          url: {
            equals: articleUrl
          }
        }
      });

      if (existingPages.results.length > 0) {
        console.log(`[${new Date().toISOString()}] 页面已存在，跳过: ${item.title || '无标题'}`);
        skippedCount++;
        continue;
      }

      // 提取文章内容
      let content = '';
      if (item.summary?.content) {
        content = item.summary.content;
        console.log(`[${new Date().toISOString()}] 提取到文章摘要，长度: ${content.length}字符`);
      } else {
        console.log(`[${new Date().toISOString()}] 未找到文章摘要`);
      }

      // 提取第一张图片URL
      let imageUrl = '';
      if (item.enclosure?.length) {
        imageUrl = item.enclosure[0].href;
        console.log(`[${new Date().toISOString()}] 提取到图片URL: ${imageUrl.substring(0, 50)}...`);
      } else {
        console.log(`[${new Date().toISOString()}] 未找到图片`);
      }

      // 处理时间戳，确保是数字类型
      const publishedTimestamp = typeof item.published === 'string' 
        ? parseInt(item.published, 10) 
        : item.published;
      console.log(`[${new Date().toISOString()}] 处理发布时间: ${publishedTimestamp ? new Date(publishedTimestamp * 1000).toISOString() : '未知'}`);

      // 创建Notion页面
      console.log(`[${new Date().toISOString()}] 开始创建Notion页面: ${item.title || '无标题'}`);
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
              start: publishedTimestamp 
                ? new Date(publishedTimestamp * 1000).toISOString()
                : new Date().toISOString()
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

      console.log(`[${new Date().toISOString()}] 成功导入: ${item.title || '无标题'}`);
      importedCount++;
    }

    console.log(`[${new Date().toISOString()}] 同步任务完成 - 成功导入: ${importedCount}篇, 跳过: ${skippedCount}篇`);
    return {
      success: true,
      message: `成功同步 ${importedCount} 篇文章到Notion，跳过 ${skippedCount} 篇`,
      imported: importedCount,
      skipped: skippedCount,
      totalProcessed: itemsToImport.length
    };
  } catch (error) {
    console.error(`[${new Date().toISOString()}] 同步过程出错:`, error.stack || error.message);
    throw new Error(`同步失败: ${error.message}`);
  }
}

// 支持GET方法
export async function GET(request) {
  console.log(`[${new Date().toISOString()}] 收到GET请求`);
  return await POST(request);
}

// 处理POST方法
export async function POST(request) {
  console.log(`[${new Date().toISOString()}] 收到POST请求`);
  try {
    const result = await syncInoreaderToNotion();
    console.log(`[${new Date().toISOString()}] 请求处理成功:`, JSON.stringify(result));
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: {
        'Content-Type': 'application/json'
      }
    });
  } catch (error) {
    console.error(`[${new Date().toISOString()}] 请求处理失败:`, error.message);
    return new Response(JSON.stringify({
      success: false,
      message: error.message
    }), {
      status: 500,
      headers: {
        'Content-Type': 'application/json'
      }
    });
  }
}
