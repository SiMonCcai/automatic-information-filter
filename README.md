# Wechat2RSS

将微信公众号文章转换为 RSS 订阅源的服务。

## 项目简介

Wechat2RSS 是一个将微信公众号文章转换为 RSS 订阅源的服务。本项目已成功部署在 VPS 服务器上，可通过 Web 界面进行管理和配置。

## 功能特性

- 📱 支持微信公众号文章订阅
- 📡 生成 RSS 订阅源
- 🌐 Web 界面管理
- 🐳 Docker 容器化部署
- 💾 数据持久化存储

## 技术栈

- **框架**: Next.js (App Router)
- **语言**: TypeScript
- **样式**: Tailwind CSS
- **部署平台**: Vercel

## 项目结构

```
automatic-information-filter/
├── app/                 # Next.js App Router
│   ├── api/          # API 路由
│   ├── favicon.ico
│   ├── globals.css
│   ├── layout.tsx
│   └── page.tsx      # 主页面
├── public/             # 静态资源
├── next.config.ts      # Next.js 配置
├── package.json
├── tsconfig.json
├── vercel.json         # Vercel 部署配置
└── README.md
```

## 快速开始

### 安装依赖

```bash
npm install
```

### 运行开发服务器

```bash
npm run dev
```

打开 [http://localhost:3000](http://localhost:3000) 查看开发环境。

## 部署

本项目使用 Vercel 进行部署。

查看 [Next.js 部署文档](https://nextjs.org/docs/app/building-your-application/deploying) 了解更多详情。

## Learn More

- [Next.js Documentation](https://nextjs.org/docs) - 学习 Next.js 特性和 API
- [Learn Next.js](https://nextjs.org/learn) - 交互式 Next.js 教程
- [Next.js GitHub Repository](https://github.com/vercel/next.js) - 欢迎反馈和贡献
