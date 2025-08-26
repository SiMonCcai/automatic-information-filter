export default async function handler(req, res) {
const url = new URL(req.url, `https://${req.headers.host}`);
const code = url.searchParams.get('code');


const clientId = process.env.INOREADER_CLIENT_ID;
const clientSecret = process.env.INOREADER_CLIENT_SECRET;
const redirectUri = process.env.INOREADER_REDIRECT_URI; // e.g. https://<your>.vercel.app/api/oauth-callback


if (!clientId || !clientSecret || !redirectUri) {
return res.status(500).send('Missing INOREADER envs');
}


// 无 code 时，引导用户去授权
if (!code) {
const authUrl = new URL('https://www.inoreader.com/oauth2/auth');
authUrl.searchParams.set('client_id', clientId);
authUrl.searchParams.set('redirect_uri', redirectUri);
authUrl.searchParams.set('response_type', 'code');
authUrl.searchParams.set('scope', 'read');
return res.status(200).send(
`<a href="${authUrl.toString()}">点击这里到 Inoreader 授权（scope=read）</a>`
);
}


// 用授权码换取 token
try {
const body = new URLSearchParams({
code,
redirect_uri: redirectUri,
client_id: clientId,
client_secret: clientSecret,
grant_type: 'authorization_code'
});


const tokenResp = await fetch('https://www.inoreader.com/oauth2/token', {
method: 'POST',
headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
body
});


const json = await tokenResp.json();
if (!tokenResp.ok) {
return res.status(500).send(`Token error: ${tokenResp.status} ${JSON.stringify(json)}`);
}


// 把 refresh_token 显示出来，复制到 Vercel 环境变量里
const { access_token, refresh_token, expires_in, token_type } = json;


return res.status(200).send(
`<pre>${JSON.stringify({ access_token, refresh_token, expires_in, token_type }, null, 2)}</pre>` +
'<p>请复制 <code>refresh_token</code> 保存到 Vercel 环境变量 INOREADER_REFRESH_TOKEN。</p>'
);
} catch (e) {
return res.status(500).send(`Exchange failed: ${e}`);
}
}