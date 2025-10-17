// 引入 Upstash KV 存储
import { Redis } from '@upstash/redis';

// 初始化 Upstash Redis 客户端（使用 Vercel 自动生成的 KV_ 前缀变量）
const redis = new Redis({
  url: process.env.KV_REST_API_URL,
  token: process.env.KV_REST_API_TOKEN,
});

/**
 * 生成随机字符串作为 OAuth state 参数
 * @returns 随机字符串
 */
const generateState = (): string => {
  return Math.random().toString(36).substring(2, 15) + 
         Math.random().toString(36).substring(2, 15);
};

/**
 * 创建 HTML 响应
 * @param title 页面标题
 * @param content 页面内容
 * @param status 状态码
 * @returns Response 对象
 */
const createHtmlResponse = (title: string, content: string, status: number): Response => {
  return new Response(`
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="UTF-8">
        <title>${title}</title>
        <style>
          body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 2rem; line-height: 1.6; }
          .container { text-align: center; padding: 2rem 0; }
          .card { background: #f3f4f6; padding: 1.5rem; border-radius: 8px; margin: 1rem 0; text-align: left; }
          .btn { display: inline-block; padding: 12px 24px; background: #2563eb; color: white; text-decoration: none; border-radius: 4px; margin: 1rem 0; }
          .error { color: #dc2626; }
          pre { overflow-x: auto; white-space: pre-wrap; word-wrap: break-word; }
        </style>
      </head>
      <body>
        <div class="container">
          <h2>${title}</h2>
          ${content}
        </div>
      </body>
    </html>
  `, { 
    status, 
    headers: { 'Content-Type': 'text/html; charset=utf-8' } 
  });
};

// 处理GET请求（OAuth回调默认使用GET方法）
export async function GET(request: Request) {
  try {
    // 1. 基础参数校验
    const clientId = process.env.INOREADER_CLIENT_ID;
    const clientSecret = process.env.INOREADER_CLIENT_SECRET;
    const redirectUri = process.env.INOREADER_REDIRECT_URI;
    
    if (!clientId || !clientSecret || !redirectUri) {
      return createHtmlResponse(
        '配置错误',
        `<p>请先配置环境变量：INOREADER_CLIENT_ID、INOREADER_CLIENT_SECRET、INOREADER_REDIRECT_URI</p>`,
        500
      );
    }

    // 2. 处理授权流程
    const url = new URL(request.url);
    const code = url.searchParams.get('code');
    const state = url.searchParams.get('state');
    const baseCallbackUrl = `${url.origin}/api/oauth-callback`;

    if (!code) {
      // 无授权码时，生成state并引导用户跳转Inoreader授权页
      const newState = generateState();
      await redis.set(`oauth_state:${newState}`, 'valid', { ex: 600 }); // 10分钟有效期
      
      const authUrl = new URL('https://www.inoreader.com/oauth2/auth');
      authUrl.searchParams.set('client_id', clientId);
      authUrl.searchParams.set('redirect_uri', redirectUri);
      authUrl.searchParams.set('response_type', 'code');
      authUrl.searchParams.set('scope', 'read');
      authUrl.searchParams.set('state', newState);

      return createHtmlResponse(
        'Inoreader 授权',
        `
          <p>点击下方按钮授权，完成后将自动保存令牌</p>
          <a href="${authUrl.toString()}" class="btn">前往 Inoreader 授权</a>
        `,
        200
      );
    } else {
      // 有授权码时：验证state + 交换令牌 + 存入Upstash KV
      if (!state) {
        return createHtmlResponse(
          '授权失败',
          `
            <p class="error">缺少state参数，可能存在安全风险</p>
            <p><a href="${baseCallbackUrl}" class="btn">返回重试</a></p>
          `,
          400
        );
      }

      // 验证state有效性
      const stateValid = await redis.get(`oauth_state:${state}`);
      if (!stateValid) {
        return createHtmlResponse(
          '授权失败',
          `
            <p class="error">state验证失败，可能存在安全风险或授权已过期</p>
            <p><a href="${baseCallbackUrl}" class="btn">返回重试</a></p>
          `,
          403
        );
      }

      // 验证通过后删除state
      await redis.del(`oauth_state:${state}`);

      // 调用Inoreader接口交换令牌
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
        const errorMsg = tokenData.error || tokenData.error_description || 
                        `未知错误（状态码：${tokenResp.status}）`;
        return createHtmlResponse(
          '令牌交换失败',
          `
            <p class="error">错误信息：${errorMsg}</p>
            <p><a href="${baseCallbackUrl}" class="btn">重试</a></p>
          `,
          500
        );
      }

      // 验证令牌数据完整性
      const { access_token, refresh_token, expires_in, token_type } = tokenData;
      if (!access_token || !refresh_token || !expires_in || !token_type) {
        return createHtmlResponse(
          '令牌数据不完整',
          `
            <p>从Inoreader获取的令牌数据不完整</p>
            <p><a href="${baseCallbackUrl}" class="btn">返回重试</a></p>
          `,
          500
        );
      }
      
      // 准备存储的令牌数据
      const tokenStoreData = {
        accessToken: access_token,
        refreshToken: refresh_token,
        tokenType: token_type,
        expiresAt: Date.now() + expires_in * 1000,
        updatedAt: Date.now()
      };
      
      // 存储令牌到Upstash KV
      const setResult = await redis.set('inoreader_tokens', JSON.stringify(tokenStoreData));
      if (!setResult) {
        throw new Error('存储令牌到Upstash KV失败');
      }
      await redis.expire('inoreader_tokens', expires_in);

      // 授权成功响应
      return createHtmlResponse(
        '授权成功！令牌已自动保存',
        `
          <div class="card">
            <pre>${JSON.stringify(tokenStoreData, null, 2)}</pre>
          </div>
          <p style="margin-top: 1rem; color: #6b7280;">
            1. 无需手动复制令牌（已存入Upstash KV，定时任务会自动读取）<br>
            2. 后续刷新令牌会自动更新此数据<br>
            3. 可前往 <a href="https://console.upstash.com/redis" target="_blank">Upstash控制台</a> 查看令牌
          </p>
        `,
        200
      );
    }
  } catch (error) {
    console.error('授权过程出错:', error);
    const url = new URL(request.url);
    return createHtmlResponse(
      '系统错误',
      `
        <p class="error">错误信息：${(error as Error).message}</p>
        <p><a href="${url.origin}/api/oauth-callback" class="btn">重试</a></p>
      `,
      500
    );
  }
}
