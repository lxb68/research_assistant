# Backend

这里是独立 Python 后端服务目录，使用 FastAPI 提供文献搜索接口。

## 安装依赖

```powershell
cd E:\research_agent\backend
python -m pip install -r requirements.txt
```

## 运行方式

在项目根目录运行开发服务：

```powershell
npm.cmd run backend:dev
```

或者进入后端目录直接运行：

```powershell
cd E:\research_agent\backend
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 4000
```

默认服务地址：

```text
http://127.0.0.1:4000
```

FastAPI 自动文档：

```text
http://127.0.0.1:4000/docs
http://127.0.0.1:4000/redoc
```

## 当前接口

```text
GET /api/health
GET /api/papers/sources
GET /api/papers/search?source=arxiv&q=llm&limit=10
GET /api/papers/search?source=pubmed&q=cancer&limit=10
GET /api/papers/search?source=crossref&q=large%20language%20model&limit=10
GET /api/papers/search?source=ieee&q=transformer&limit=10
```

## 目录说明

```text
backend/
├─ app/
│  ├─ main.py                         # FastAPI 应用入口
│  ├─ core/
│  │  └─ config.py                    # 环境变量和运行配置
│  ├─ schemas/
│  │  └─ paper.py                     # 文献返回结构标准化
│  ├─ services/
│  │  ├─ paper_search.py              # 文献搜索分发服务
│  │  └─ providers/
│  │     ├─ arxiv.py                  # arXiv 搜索
│  │     ├─ pubmed.py                 # PubMed 搜索
│  │     ├─ crossref.py               # Crossref 搜索
│  │     └─ ieee.py                   # IEEE Xplore 搜索
│  └─ utils/
│     ├─ http.py                      # 第三方 HTTP 请求工具
│     └─ text.py                      # 文本清理工具
├─ prisma/
│  └─ schema.prisma                   # 数据库模型定义，后续需要时启用
├─ .env.example                       # 环境变量示例
└─ requirements.txt                   # Python 依赖
```

## 环境变量

复制 `.env.example` 为 `.env` 后按需填写：

```env
HOST=127.0.0.1
PORT=4000
CORS_ORIGIN=http://localhost:3000
NCBI_EMAIL=
NCBI_API_KEY=
IEEE_API_KEY=
SEMANTIC_SCHOLAR_API_KEY=
REQUEST_TIMEOUT=15
```

说明：

- `arxiv` 不需要 API Key。
- `pubmed` 可以不填 Key，但建议填写 `NCBI_EMAIL`；高频调用时再申请 `NCBI_API_KEY`。
- `crossref` 不需要 API Key。
- `ieee` 需要先申请并配置 `IEEE_API_KEY`。
