# LivroTrack 📚

Histórico de preços de livros na Amazon Brasil. Descubra se o preço realmente vale a pena.

## Funcionalidades

- 📈 Gráfico histórico de preços (até 1 ano)
- 🔍 Busca por título, autor ou ISBN
- 🔔 Alertas de preço por email
- 📊 Análise de padrões sazonais (Black Friday, etc.)
- ✅ Veredicto automático: comprar agora ou esperar?

---

## Estrutura do projeto

```
livrotrack/
├── database/
│   └── schema.sql          # Schema PostgreSQL
├── backend/
│   ├── main.py             # API FastAPI
│   ├── requirements.txt
│   └── Dockerfile
├── scraper/
│   ├── scraper.py          # Coleta preços na Amazon
│   └── Dockerfile
├── frontend/
│   └── index.html          # Interface web completa
└── docker-compose.yml
```

---

## Configuração e deploy

### 1. Pré-requisitos

- Docker e Docker Compose instalados
- (Recomendado) Conta em um serviço de proxy residencial (Bright Data, Oxylabs ou Webshare)
- Conta no [Resend](https://resend.com) para envio de emails de alerta

### 2. Variáveis de ambiente

Crie um arquivo `.env` na raiz do projeto:

```env
DB_PASSWORD=senha_forte_aqui
PROXY_URL=http://usuario:senha@proxy.exemplo.com:porta
EMAIL_API_KEY=re_suachaveresend
SCRAPE_INTERVAL_HOURS=8
```

### 3. Subir com Docker

```bash
docker-compose up -d
```

Isso vai:
- Subir o PostgreSQL e rodar o schema automaticamente
- Iniciar a API na porta `8000`
- Iniciar o scraper (roda a cada 8h)
- Servir o frontend na porta `80`

Acesse `http://localhost` para ver o site.

### 4. Deploy em produção

**Backend (API + Scraper):** Railway, Render, ou uma VPS simples (DigitalOcean, Hetzner)

```bash
# Railway (mais fácil)
npm install -g railway
railway login
railway up
```

**Banco de dados:** Supabase (grátis até 500MB) ou Railway PostgreSQL

**Frontend:** Vercel, Netlify ou Cloudflare Pages — é só um HTML estático.

```bash
# Netlify
npx netlify-cli deploy --dir=frontend --prod
```

---

## Adicionar livros para monitoramento

### Via API

```bash
# Adicionar um livro pelo ASIN da Amazon
curl -X POST http://localhost:8000/api/books/B07VXZ5RXB/track

# Buscar livros
curl "http://localhost:8000/api/books/search?q=senhor+dos+aneis"

# Ver histórico
curl "http://localhost:8000/api/books/B07VXZ5RXB/history?days=365"
```

### Via script Python

```python
import asyncio
from scraper.scraper import AmazonScraper

async def main():
    scraper = AmazonScraper()
    
    # Buscar e adicionar vários livros
    results = await scraper.search_books("Harry Potter")
    for r in results:
        print(f"{r.asin} | {r.title} | R$ {r.price}")

asyncio.run(main())
```

---

## Como encontrar o ASIN de um livro

O ASIN está na URL do produto na Amazon:
```
https://www.amazon.com.br/dp/B07VXZ5RXB
                              ^^^^^^^^^^
                              Este é o ASIN
```

---

## Anti-bloqueio da Amazon

A Amazon bloqueia scrapers agressivos. Estratégias implementadas:

1. **Rotação de User-Agents** — já incluída no código
2. **Delay aleatório** — 2 a 5 segundos entre requisições, 8 a 15 segundos entre livros
3. **Proxy residencial** — recomendado para produção com muitos livros
4. **Intervalos longos** — coleta a cada 6–12h, não minuto a minuto

Para proxy, o [Webshare](https://webshare.io) tem plano gratuito de 10 proxies que já funciona bem.

---

## Integração com API de Afiliados Amazon

Para uma solução mais robusta e legal, use a **Product Advertising API** da Amazon:

1. Crie conta no [Programa de Afiliados Amazon Brasil](https://associados.amazon.com.br)
2. Solicite acesso à PA API (requer vendas mínimas após 90 dias)
3. Substitua o scraper pelas chamadas de API:

```python
import boto3

paapi = boto3.client(
    'paapi5',
    region_name='us-east-1',
    aws_access_key_id='SUA_KEY',
    aws_secret_access_key='SEU_SECRET',
)
```

---

## Roadmap

- [ ] App mobile (PWA)
- [ ] Comparação entre edições (digital vs. físico)
- [ ] Integração com Livraria Cultura e Estante Virtual
- [ ] Extensão para Chrome que mostra o histórico inline na Amazon
- [ ] API pública para desenvolvedores

---

## Tech stack

| Camada | Tecnologia |
|--------|-----------|
| Frontend | HTML/CSS/JS vanilla + Chart.js |
| Backend | FastAPI (Python) |
| Banco | PostgreSQL |
| Scraping | httpx + BeautifulSoup |
| Deploy | Docker Compose |
| Email | Resend |

---

## Licença

MIT — use como quiser.
