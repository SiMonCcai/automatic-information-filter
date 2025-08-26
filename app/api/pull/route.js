import { Client } from '@notionhq/client';
function pickPublished(item) {
// Inoreader 的 published 通常是 Unix 秒
if (item?.published) return new Date(item.published * 1000).toISOString();
return new Date().toISOString();
}


const notion = new Client({ auth: NOTION_TOKEN });


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


export default async function handler(req, res) {
try {
if (!NOTION_TOKEN || !NOTION_DATABASE_ID) {
return res.status(500).json({ error: 'Missing NOTION envs' });
}


const accessToken = await getAccessToken();
const items = await fetchInoreaderItems(accessToken);


let imported = 0;
for (const item of items) {
if (imported >= MAX_IMPORT) break;


const url = pickUrl(item);
const title = item?.title || '';
const published = pickPublished(item);
const source = url ? new URL(url).hostname : 'inoreader';
const content = cleanContent(pickContent(item));


const exists = await pageExistsByUrl(NOTION_DATABASE_ID, url);
if (exists) continue; // 去重


await createNotionPage(NOTION_DATABASE_ID, { title, url, published, source, content });
imported++;
}


return res.status(200).json({ ok: true, imported });
} catch (e) {
return res.status(500).json({ ok: false, error: String(e) });
}
}