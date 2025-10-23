// /app/api/oauth-callback/route.ts

import { Redis } from '@upstash/redis';

// 初始化 Upstash Redis 客户端
const redis = new Redis({
  url: process.env.KV_REST_API_URL!,
  token: process.env.KV_REST_API_TOKEN!,
});

const generateState = (): string => {
  return Math.random().toString(36).substring(2, 15);
};

const createHtmlResponse = (title: string, content: string, status: number): Response => {
  return new Response(`
    <!DOCTYPE html>
    <html>
      <head><meta charset="UTF-8"><title>${title}</title><style>body{font-family:Arial,sans-serif;max-width:800px;margin:0 auto;padding:2rem;line-height:1.6;}.container{text-align:center;padding:2rem 0;}.card{background:#f3f4f6;padding:1.5rem;border-radius:8px;margin:1rem 0;text-align:left;}.btn{display:inline-block;padding:12px 24px;background:#2563eb;color:white;text-decoration:none;border-radius:4px;margin:1rem 0;}.error{color:#dc2626;}pre{overflow-x:auto;white-space:pre-wrap;word-wrap:break-word;}</style></head>
      <body><div class="container"><h2>${title}</h2>${content}</div></body>
    </html>
  `, { 
    status, 
    headers: { 'Content-Type': 'text/html; charset=utf-8' } 
  });
};

export async function GET(request: Request) {
  try {
    const clientId = process.env.INOREADER_CLIENT_ID;
    const clientSecret = process.env.INOREADER_CLIENT_SECRET;
    const redirectUri = process.env.INOREADER_REDIRECT_URI;
    
    if (!clientId || !clientSecret || !redirectUri) {
      return createHtmlResponse('配置错误', '<p>请先配置环境变量</p>', 500);
    }

    const url = new URL(request.url);
    const code = url.searchParams.get('code');
    const state = url.searchParams.get('state');
    const baseCallbackUrl = `${url.origin}/api/oauth-callback`;

    // 检查 Inoreader 是否返回了错误
    const error = url.searchParams.get('error');
    if (error) {
        const errorDesc = url.searchParams.get('error_description') || '未知授权错误';
        return createHtmlResponse('授权失败', `<p class="error">Inoreader 返回错误: ${errorDesc}</p><p><a href="${baseCallbackUrl}" class="btn">重试</a></p>`, 400);
    }

    if (!code) {
      const newState = generateState();
      await redis.set(`oauth_state:${newState}`, 'valid', { ex: 600 });
      
      const authUrl = new URL('https://www.inoreader.com/oauth2/auth');
      authUrl.searchParams.set('client_id', clientId);
      authUrl.searchParams.set('redirect_uri', redirectUri);
      authUrl.searchParams.set('response_type', 'code');
      
      // *** 关键修复：将授权范围改回 'read' ***
      authUrl.searchParams.set('scope', 'read'); 

      authUrl.searchParams.set('state', newState);

      return createHtmlResponse('Inoreader 授权', `<p>点击下方按钮授权</p><a href="${authUrl.toString()}" class="btn">前往 Inoreader 授权</a>`, 200);
    } else {
      if (!state || !(await redis.get(`oauth_state:${state}`))) {
        return createHtmlResponse('授权失败', '<p class="error">state验证失败</p>', 403);
      }
      await redis.del(`oauth_state:${state}`);

      const tokenResp = await fetch('https://www.inoreader.com/oauth2/token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          code,
          redirect_uri: redirectUri,
          client_id: clientId,
          client_secret: clientSecret,
          grant_type: 'authorization_code'
        })
      });
      const tokenData = await tokenResp.json();

      if (!tokenResp.ok) {
        throw new Error(tokenData.error_description || '令牌交换失败');
      }
      
      const { access_token, refresh_token, expires_in, token_type } = tokenData;
      if (!access_token || !refresh_token) {
        throw new Error('从Inoreader获取的令牌数据不完整');
      }
      
      const tokenStoreData = {
        accessToken: access_token,
        refreshToken: refresh_token,
        tokenType: token_type,
        expiresAt: Date.now() + expires_in * 1000,
        updatedAt: Date.now()
      };
      
      await redis.set('inoreader_tokens', JSON.stringify(tokenStoreData));
      
      // 确保 redis.expire 不再被调用

      return createHtmlResponse('授权成功！令牌已永久保存', `<div class="card"><pre>${JSON.stringify(tokenStoreData, null, 2)}</pre></div>`, 200);
    }
  } catch (error) {
    console.error('授权过程出错:', error);
    return createHtmlResponse('系统错误', `<p class="error">${(error as Error).message}</p>`, 500);
  }
}