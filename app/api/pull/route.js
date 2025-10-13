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
  try {
    // 1. 从Redis获取Inoreader令牌
    const tokenDataStr = await redis.get('inoreader_tokens');
    if (!tokenDataStr) {
      throw new Error('未找到Inoreader令牌，请先完成授权');
    }
    
    const tokenData = JSON.parse(tokenDataStr);
    const { accessToken } = tokenData;
    
    // 检查令牌是否过期
    if (Date.now() > tokenData.expiresAt) {
      throw new Error('Inoreader令牌已过期，请重新授权');
    }

    // 2. 从Inoreader拉取星标文章
    const starredItemsResponse = await fetch(
      'https://www.inoreader.com/reader/api/0/stream/contents/user/-/state/com.google/starred',
      {
        headers: {
          'Authorization': `Bearer ${accessToken}`,
        },
      }
    );

    if (!starredItemsResponse.ok) {
      throw new Error(`拉取Inoreader数据失败: ${starredItemsResponse.status} ${starredItemsResponse.statusText}`);
    }

    const starredData = await starredItemsResponse.json();
    const items = starredData.items || [];
    
    // 限制导入数量，避免一次性导入过多
    const MAX_IMPORT = 10;
    const itemsToImport = items.slice(0, MAX_IMPORT);

    // 3. 同步到Notion数据库
    const databaseId = process.env.NOTION_DATABASE_ID;
    if (!databaseId) {
      throw new Error('未配置Notion数据库ID');
    }

    for (const item of itemsToImport) {
      // 获取文章URL，增加容错处理
      const articleUrl = item.canonical?.[0]?.href || item.alternate?.[0]?.href;
      if (!articleUrl) {
        console.log(`文章URL不存在，跳过: ${item.title || '无标题'}`);
        continue;
      }

      // 检查是否已存在相同URL的页面
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
        console.log(`页面已存在，跳过: ${item.title || '无标题'}`);
        continue;
      }

      // 提取文章内容
      let content = '';
      if (item.summary?.content) {
        content = item.summary.content;
      }

      // 提取第一张图片URL
      let imageUrl = '';
      if (item.enclosure?.length) {
        imageUrl = item.enclosure[0].href;
      }

      // 处理时间戳，确保是数字类型
      const publishedTimestamp = typeof item.published === 'string' 
        ? parseInt(item.published, 10) 
        : item.published;

      // 创建Notion页面
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

      console.log(`成功导入: ${item.title || '无标题'}`);
    }

    return {
      success: true,
      message: `成功同步 ${itemsToImport.length} 篇文章到Notion`,
      count: itemsToImport.length
    };
  } catch (error) {
    console.error('同步过程出错:', error);
    throw new Error(`同步失败: ${error.message}`);
  }
}

// 支持GET方法
export async function GET(request) {
  return await POST(request);
}

// 处理POST方法
export async function POST(request) {
  try {
    const result = await syncInoreaderToNotion();
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: {
        'Content-Type': 'application/json'
      }
    });
  } catch (error) {
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
