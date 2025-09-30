// 极简测试路由：仅返回成功响应
export async function GET() {
  return new Response('OAuth callback route is working!', {
    status: 200,
    headers: { 'Content-Type': 'text/plain' }
  });
}
