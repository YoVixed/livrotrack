"""
LivroTrack API — FastAPI backend
"""

from datetime import datetime, timedelta
from typing import Optional
import os
import asyncpg
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr

app = FastAPI(title="LivroTrack API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost/livrotrack")
db_pool: asyncpg.Pool = None


@app.on_event("startup")
async def startup():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)


@app.on_event("shutdown")
async def shutdown():
    await db_pool.close()


# ─── Models ───────────────────────────────────────────────────────────────────

class AlertCreate(BaseModel):
    book_id: str
    email: EmailStr
    target_price: float


class SearchRequest(BaseModel):
    query: str


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health_check():
    """Verifica se a API está funcionando"""
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/api/books/search")
async def search_books(
    q: str = Query(..., min_length=2, description="Título, autor ou ISBN"),
):
    """
    Busca livros no banco local
    """
    if not db_pool:
        raise HTTPException(500, "Banco de dados não conectado")
    
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT b.id, b.asin, b.title, b.author, b.cover_url, b.amazon_url,
                       COALESCE(blp.current_price, 0) as current_price, 
                       COALESCE(blp.in_stock, false) as in_stock,
                       COALESCE(bps.min_price, 0) as min_price,
                       COALESCE(bps.max_price, 0) as max_price,
                       COALESCE(bps.avg_price, 0) as avg_price
                FROM books b
                LEFT JOIN book_latest_price blp ON b.id = blp.book_id
                LEFT JOIN book_price_stats bps ON b.id = bps.book_id
                WHERE b.title ILIKE $1 OR b.author ILIKE $1 OR b.isbn = $2
                LIMIT 20
            """, f"%{q}%", q.strip())

            return {
                "source": "local",
                "results": [dict(r) for r in rows] if rows else []
            }
    except Exception as e:
        raise HTTPException(500, f"Erro ao buscar livros: {str(e)}")


@app.get("/api/books/{asin}")
async def get_book(asin: str):
    """Retorna dados completos de um livro com histórico de preços"""
    if not db_pool:
        raise HTTPException(500, "Banco de dados não conectado")
    
    try:
        async with db_pool.acquire() as conn:
            book = await conn.fetchrow("""
                SELECT b.id, b.asin, b.title, b.author, b.publisher, b.isbn,
                       b.cover_url, b.amazon_url,
                       COALESCE(blp.current_price, 0) as current_price,
                       COALESCE(blp.original_price, 0) as original_price,
                       COALESCE(blp.discount_pct, 0) as discount_pct,
                       COALESCE(blp.in_stock, false) as in_stock,
                       blp.last_checked,
                       COALESCE(bps.min_price, 0) as min_price,
                       COALESCE(bps.max_price, 0) as max_price,
                       COALESCE(bps.avg_price, 0) as avg_price,
                       bps.tracking_since
                FROM books b
                LEFT JOIN book_latest_price blp ON b.id = blp.book_id
                LEFT JOIN book_price_stats bps ON b.id = bps.book_id
                WHERE b.asin = $1
                LIMIT 1
            """, asin)

            if not book:
                raise HTTPException(404, "Livro não encontrado")
            
            # Busca histórico de preços
            history = await conn.fetch("""
                SELECT price, original_price, discount_pct, in_stock, scraped_at
                FROM price_history
                WHERE book_id = $1
                ORDER BY scraped_at DESC
                LIMIT 365
            """, book['id'])

            result = dict(book)
            result['history'] = [dict(h) for h in history]
            
            # Calcula estatísticas adicionais
            if history:
                prices = [h['price'] for h in history if h['price']]
                result['price_drops_count'] = len(set(prices))
                result['best_months'] = []  # Pode ser calculado depois
            
            return result
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erro ao buscar livro: {str(e)}")


@app.post("/api/alerts")
async def create_alert(alert: AlertCreate):
    """Cria um alerta de preço para um livro"""
    if not db_pool:
        raise HTTPException(500, "Banco de dados não conectado")
    
    if alert.target_price <= 0:
        raise HTTPException(400, "Preço-alvo deve ser maior que 0")
    
    try:
        async with db_pool.acquire() as conn:
            # Verifica se o livro existe
            book_exists = await conn.fetchval(
                "SELECT id FROM books WHERE id = $1",
                alert.book_id
            )
            
            if not book_exists:
                raise HTTPException(404, "Livro não encontrado")
            
            # Cria o alerta
            alert_id = await conn.fetchval("""
                INSERT INTO price_alerts (book_id, email, target_price, is_active)
                VALUES ($1, $2, $3, true)
                RETURNING id
            """, alert.book_id, alert.email, alert.target_price)
            
            return {
                "id": alert_id,
                "status": "criado",
                "message": "Alerta criado com sucesso! Você será notificado por email."
            }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Erro ao criar alerta: {str(e)}")


@app.get("/api/trending")
async def get_trending():
    """Retorna livros com quedas recentes de preço"""
    if not db_pool:
        raise HTTPException(500, "Banco de dados não conectado")
    
    try:
        async with db_pool.acquire() as conn:
            books = await conn.fetch("""
                SELECT b.asin, b.title, b.author,
                       COALESCE(blp.current_price, 0) as current_price,
                       COALESCE(bps.max_price, blp.current_price) as prev_price,
                       ROUND((1 - COALESCE(blp.current_price, 0)::float / 
                              NULLIF(COALESCE(bps.max_price, blp.current_price), 0)) * 100) as drop_pct
                FROM books b
                LEFT JOIN book_latest_price blp ON b.id = blp.book_id
                LEFT JOIN book_price_stats bps ON b.id = bps.book_id
                WHERE COALESCE(blp.current_price, 0) > 0
                ORDER BY drop_pct DESC
                LIMIT 12
            """)
            
            return {
                "trending": [dict(b) for b in books] if books else []
            }
    except Exception as e:
        raise HTTPException(500, f"Erro ao buscar trending: {str(e)}")


@app.get("/")
async def root():
    """Endpoint raiz"""
    return {
        "nome": "LivroTrack API",
        "versao": "1.0.0",
        "status": "online",
        "documentacao": "/docs"
    }
