import { Client } from '@notionhq/client';

function pickPublished(item) {
  // Inoreader 的 published 通常是 Unix 秒
  if (item?.published) return new Date(item.published * 1000).toISOString();
  return new Date().toISOString();
}

// 提取URL的辅助函数
function pickUrl(item) {
  return item?.alternate?.[0]?.href || item?.canonical?.[0]?.href;
}

// 提取内容的辅助函数
function pickContent(item) {
  return item?.content?.content || item?.summary || '';
}

// 清洗内容的函数：移除HTML标签、图片链接、所有空格
function cleanContent(html) {
  if (!html) return '';
  
  // 1. 移除所有HTML标签
  let text = html.replace(/<[^>]*>?/gm, '');
  
  // 2. 移除图片链接（匹配各种图片格式的URL）
  text = text.replace(/https?:\/\/[^\s]*?\.(jpg|jpeg|png|gif|bmp|webp|svg)[^\s]*/gi, '');
  
  // 3. 移除所有空格（包括空格、制表符、换行符等）
  text = text.replace(/\s+/g, '');
  
  return text;
}

const notion = new Client({ auth: process.env.NOTION_TOKEN });

async function pageExistsByUrl(dbId, url) {
  if (!url) return false;
  const resp = await notion.databases.query({
    database_id: dbId,
    filter: {
      property: 'URL',
      url: { equals: url }
    },
    page_size: 1
  });
  return resp.results.length > 0;
}

async function createNotionPage(dbId, { title, url, published, source, content }) {
  // 把正文切块（Notion 单个 rich_text 块长度有限制，这里做简易切割）
  const MAX_BLOCK = 1800; // 单块最大字符
  const blocks = [];
  const text = content || '';
  for (let i = 0; i < text.length; i += MAX_BLOCK) {
    blocks.push({
      object: 'block',
      type: 'paragraph',
      paragraph: {
        rich_text: [{ type: 'text', text: { content: text.slice(i, i + MAX_BLOCK) } }]
      }
    });
    if (blocks.length >= 8) break; // 最多 8 块，避免一次写太多
  }

  return notion.pages.create({
    parent: { database_id: dbId },
    properties: {
      Name: { title: [{ text: { content: title || '(无标题)' } }] },
      URL: url ? { url } : undefined,
      Published: published ? { date: { start: published } } : undefined,
      Source: source ? { rich_text: [{ text: { content: source } }] } : undefined
    },
    children: blocks
  });
}

// 刷新令牌函数
async function refreshAccessToken(refreshToken) {
  const clientId = process.env.INOREADER_CLIENT_ID;
  const clientSecret = process.env.INOREADER_CLIENT_SECRET;
  const redirectUri = process.env.INOREADER_REDIRECT_URI;

  if (!clientId || !clientSecret || !redirectUri) {
    throw new Error('缺少Inoreader客户端配置信息');
  }

  const response = await fetch('https://www.inoreader.com/oauth2/token', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded'
    },
    body: new URLSearchParams({
      client_id: clientId,
      client_secret: clientSecret,
      grant_type: 'refresh_token',
      refresh_token: refreshToken,
      redirect_uri: redirectUri
    })
  });

  if (!response.ok) {
    throw new Error(`刷新令牌失败: ${response.statusText}`);
  }

  const data = await response.json();
  
  // 计算过期时间（当前时间 + 有效期秒数）
  const expiresAt = Date.now() + (data.expires_in * 1000);
  
  return {
    accessToken: data.access_token,
    refreshToken: data.refresh_token || refreshToken, // 有些服务可能返回新的refresh_token
    tokenType: data.token_type,
    expiresAt
  };
}

// 获取访问令牌的函数（包含自动刷新功能）
async function getAccessToken() {
  const { Redis } = await import('@upstash/redis');
  const redis = new Redis({
    url: process.env.UPSTASH_REDIS_REST_URL,
    token: process.env.UPSTASH_REDIS_REST_TOKEN,
  });
  
  const tokenData = await redis.get('inoreader_tokens');
  if (!tokenData) {
    throw new Error('未找到访问令牌，请先完成授权');
  }
  
  let parsed = JSON.parse(tokenData);
  
  // 检查令牌是否即将过期（提前300秒刷新，避免边缘情况）
  if (parsed.expiresAt < Date.now() + 300000) {
    console.log('令牌即将过期或已过期，尝试刷新...');
    try {
      // 调用刷新令牌函数
      const newTokenData = await refreshAccessToken(parsed.refreshToken);
      // 保存新的令牌数据到Redis
      await redis.set('inoreader_tokens', JSON.stringify(newTokenData));
      parsed = newTokenData;
      console.log('令牌刷新成功');
    } catch (error) {
      console.error('令牌刷新失败:', error);
      throw new Error('令牌刷新失败，请重新授权');
    }
  }
  
  return `${parsed.tokenType} ${parsed.accessToken}`;
}

// 获取Inoreader内容的函数
async function fetchInoreaderItems(accessToken) {
  const MAX_IMPORT = process.env.MAX_IMPORT ? parseInt(process.env.MAX_IMPORT) : 5; // 从环境变量获取最大导入数量
  const apiUrl = `https://www.inoreader.com/reader/api/0/stream/contents/user/-/state/com.google/starred?n=${MAX_IMPORT}`;
  
  const response = await fetch(apiUrl, {
    headers: {
      'Authorization': accessToken
    }
  });
  
  if (!response.ok) {
    throw new Error(`获取Inoreader内容失败: ${response.statusText}`);
  }
  
  const data = await response.json();
  return data.items || [];
}

export default async function handler(req, res) {
  try {
    if (!process.env.NOTION_TOKEN || !process.env.NOTION_DATABASE_ID) {
      return res.status(500).json({ error: 'Missing NOTION envs' });
    }

    const accessToken = await getAccessToken();
    const items = await fetchInoreaderItems(accessToken);
    const MAX_IMPORT = process.env.MAX_IMPORT ? parseInt(process.env.MAX_IMPORT) : 5;

    let imported = 0;
    for (const item of items) {
      if (imported >= MAX_IMPORT) break;

      const url = pickUrl(item);
      const title = item?.title || '';
      const published = pickPublished(item);
      const source = url ? new URL(url).hostname : 'inoreader';
      const content = cleanContent(pickContent(item));

      const exists = await pageExistsByUrl(process.env.NOTION_DATABASE_ID, url);
      if (exists) continue; // 去重

      await createNotionPage(process.env.NOTION_DATABASE_ID, { title, url, published, source, content });
      imported++;
    }

    return res.status(200).json({ ok: true, imported });
  } catch (e) {
    return res.status(500).json({ ok: false, error: String(e) });
  }
}