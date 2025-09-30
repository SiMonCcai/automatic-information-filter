// 引入 Upstash KV 存储（替换 Vercel KV）
import { Redis } from '@upstash/redis';

// 初始化 Upstash Redis 客户端（从环境变量读取配置）
const redis = new Redis({
  url: process.env.UPSTASH_REDIS_REST_URL,
  token: process.env.UPSTASH_REDIS_REST_TOKEN,
});

export default async function handler(req, res) {
  try {
    // 1. 基础参数校验
    const clientId = process.env.INOREADER_CLIENT_ID;
    const clientSecret = process.env.INOREADER_CLIENT_SECRET;
    const redirectUri = process.env.INOREADER_REDIRECT_URI;
    
    if (!clientId || !clientSecret || !redirectUri) {
      return res.status(500).send(`
        <h3>配置错误</h3>
        <p>请先配置环境变量：INOREADER_CLIENT_ID、INOREADER_CLIENT_SECRET、INOREADER_REDIRECT_URI</p>
      `);
    }

    // 2. 处理授权流程
    const url = new URL(req.url, `https://${req.headers.host}`);
    const code = url.searchParams.get('code');
    const state = url.searchParams.get('state');

    if (!code) {
      // 无授权码时，生成state并引导用户跳转Inoreader授权页
      const newState = Math.random().toString(36).substring(2, 15);
      
      // 存储state用于后续验证，有效期10分钟
      await redis.set(`oauth_state:${newState}`, 'valid', { ex: 600 });
      
      const authUrl = new URL('https://www.inoreader.com/oauth2/auth');
      authUrl.searchParams.set('client_id', clientId);
      authUrl.searchParams.set('redirect_uri', redirectUri);
      authUrl.searchParams.set('response_type', 'code');
      authUrl.searchParams.set('scope', 'read');
      authUrl.searchParams.set('state', newState);
      
      return res.status(200).send(`
        <!DOCTYPE html>
        <html>
          <body style="text-align: center; padding-top: 50px; font-family: Arial;">
            <h2>Inoreader 授权</h2>
            <p>点击下方按钮授权，完成后将自动保存令牌</p>
            <a 
              href="${authUrl.toString()}" 
              style="display: inline-block; padding: 12px 24px; background: #2563eb; color: white; text-decoration: none; border-radius: 4px; margin-top: 20px;"
            >
              前往 Inoreader 授权
            </a>
          </body>
        </html>
      `);
    } else {
      // 有授权码时：验证state + 交换令牌 + 存入Upstash KV
      
      // 验证state防止CSRF攻击
      if (!state) {
        return res.status(400).send(`
          <h3>授权失败</h3>
          <p>缺少state参数，可能存在安全风险</p>
          <p><a href="${url.origin}/api/oauth-callback">返回重试</a></p>
        `);
      }
      
      const stateValid = await redis.get(`oauth_state:${state}`);
      if (!stateValid) {
        return res.status(403).send(`
          <h3>授权失败</h3>
          <p>state验证失败，可能存在安全风险或授权已过期</p>
          <p><a href="${url.origin}/api/oauth-callback">返回重试</a></p>
        `);
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
        const errorMsg = tokenData.error || tokenData.error_description || `未知错误（状态码：${tokenResp.status}）`;
        return res.status(500).send(`
          <h3>令牌交换失败</h3>
          <p>错误信息：${errorMsg}</p>
          <p>请返回重新授权 <a href="${url.origin}/api/oauth-callback">重试</a></p>
        `);
      }

      // 提取令牌并添加过期时间
      const { access_token, refresh_token, expires_in, token_type } = tokenData;
      
      if (!access_token || !refresh_token || !expires_in || !token_type) {
        return res.status(500).send(`
          <h3>令牌数据不完整</h3>
          <p>从Inoreader获取的令牌数据不完整</p>
          <p><a href="${url.origin}/api/oauth-callback">返回重试</a></p>
        `);
      }
      
      const tokenStoreData = {
        accessToken: access_token,
        refreshToken: refresh_token,
        tokenType: token_type,
        expiresAt: Date.now() + expires_in * 1000,
        updatedAt: Date.now()
      };

      // 存入Upstash KV
      const setResult = await redis.set('inoreader_tokens', JSON.stringify(tokenStoreData));
      if (!setResult) {
        throw new Error('存储令牌到Upstash KV失败');
      }
      
      // 设置键过期时间（与令牌过期时间一致，单位秒）
      await redis.expire('inoreader_tokens', expires_in);

      // 向用户展示结果
      return res.status(200).send(`
        <!DOCTYPE html>
        <html>
          <body style="text-align: center; padding-top: 50px; font-family: Arial;">
            <h2>授权成功！令牌已自动保存</h2>
            <div style="max-width: 800px; margin: 0 auto; text-align: left; background: #f3f4f6; padding: 20px; border-radius: 8px; overflow-x: auto;">
              <pre>${JSON.stringify(tokenStoreData, null, 2)}</pre>
            </div>
            <p style="margin-top: 20px; color: #6b7280;">
              1. 无需手动复制令牌（已存入Upstash KV，定时任务会自动读取）<br>
              2. 后续刷新令牌会自动更新此数据<br>
              3. 可前往 <a href="https://console.upstash.com/redis" target="_blank">Upstash控制台</a> 查看令牌
            </p>
          </body>
        </html>
      `);
    }
  } catch (error) {
    console.error('授权过程出错:', error);
    return res.status(500).send(`
      <h3>系统错误</h3>
      <p>错误信息：${error.message}</p>
      <p>请返回重新授权 <a href="${new URL(req.url, `https://${req.headers.host}`).origin}/api/oauth-callback">重试</a></p>
    `);
  }
}