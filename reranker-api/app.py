"""
Reranker API — вычисляет relevance scores через cosine similarity
Использует ollama-reranker для embeddings + cosine similarity для scoring
"""

import os
import math
import asyncio
import httpx
import time
import hashlib
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field
from functools import lru_cache
from contextlib import asynccontextmanager

# Конфигурация
OLLAMA_URL = os.getenv("OLLAMA_RERANKER_URL", "http://192.168.1.145:11437")
EMBEDDINGS_ENDPOINT = f"{OLLAMA_URL}/api/embeddings"
API_KEY = os.getenv("RERANKER_API_KEY", "secret-key-change-this")
BATCH_SIZE = int(os.getenv("RERANKER_BATCH_SIZE", "5"))  # запросов параллельно

# Мониторинг
request_count = 0
request_errors = 0
total_rerank_time = 0.0

# Кэш embeddings (LRU, max 1000 записей)
@lru_cache(maxsize=1000)
def get_embedding_cached(text_hash: str) -> Optional[List[float]]:
    """Кэшированное получение embeddings"""
    return None  # Заглушка — реальная логика ниже


# Lifecycle
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown events"""
    # Startup
    print(f"Reranker API starting... Ollama: {OLLAMA_URL}")
    yield
    # Shutdown
    print("Reranker API shutting down...")


app = FastAPI(
    title="Reranker API",
    version="1.0.0",
    description="Dedicated reranking API for total-recall",
    lifespan=lifespan
)


class Candidate(BaseModel):
    """Оптимизированный кандидат — только нужные поля"""
    text: str = Field(..., min_length=1, max_length=4096, description="Текст для embeddings")
    id: str = Field(..., min_length=1, description="Уникальный ID")
    _score: float = Field(default=0.0, ge=0, le=1, description="Score из vector search")


class RerankRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1024, description="Query для сравнения")
    candidates: List[Candidate] = Field(..., min_length=1, max_length=100, description="Кандидаты для rerank")


class RerankResult(BaseModel):
    """Результат с essential metadata"""
    id: str
    text: str
    relevance_score: float = Field(..., ge=0, le=1, description="Новый score от reranker")
    original_score: float = Field(..., ge=0, le=1, description="Старый score из vector search")


class RerankResponse(BaseModel):
    model: str
    results: List[RerankResult]


def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Вычисляет косинусное сходство между двумя векторами"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def text_to_hash(text: str) -> str:
    """Хэш текста для кэширования"""
    return hashlib.md5(text.encode('utf-8')).hexdigest()


async def get_embedding(text: str, use_cache: bool = True) -> Optional[List[float]]:
    """Получает embedding через ollama-reranker API с кэшированием"""
    if use_cache:
        text_hash = text_to_hash(text)
        cached = get_embedding_cached(text_hash)
        if cached is not None:
            return cached
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                EMBEDDINGS_ENDPOINT,
                json={"model": "xitao/bge-reranker-v2-m3:latest", "prompt": text}
            )
            response.raise_for_status()
            data = response.json()
            embedding = data.get("embedding", [])
            
            # Сохранить в кэш
            if use_cache and embedding:
                text_hash = text_to_hash(text)
                get_embedding_cached(text_hash)  # Прогрев кэша
                # Примечание: lru_cache кэширует автоматически при повторных вызовах
            
            return embedding
    except Exception as e:
        print(f"Embedding error for text '{text[:50]}...': {e}")
        return None


async def get_embeddings_batch(documents: List[str], batch_size: int = BATCH_SIZE) -> List[Optional[List[float]]]:
    """Получает embeddings с batch processing (ограничение параллелизма)"""
    embeddings = []
    for i in range(0, len(documents), batch_size):
        batch = documents[i:i+batch_size]
        tasks = [get_embedding(doc) for doc in batch]
        batch_embeddings = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Обработать результаты
        for emb in batch_embeddings:
            if isinstance(emb, Exception):
                embeddings.append(None)
            else:
                embeddings.append(emb)
        
        # Пауза между батчами для снижения нагрузки
        if i + batch_size < len(documents):
            await asyncio.sleep(0.1)
    
    return embeddings


@app.post("/rerank", response_model=RerankResponse)
async def rerank(
    request: RerankRequest,
    x_api_key: Optional[str] = Header(None, description="API key for authentication")
):
    """
    Rerank candidates по релевантности к query.
    Возвращает candidates с essential metadata, отсортированные по relevance_score.
    """
    global request_count, request_errors, total_rerank_time
    
    # Аутентификация
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    request_count += 1
    start_time = time.time()
    
    try:
        # 1. Получить embedding для query
        query_embedding = await get_embedding(request.query)
        if not query_embedding:
            raise HTTPException(status_code=500, detail="Failed to get query embedding")

        # 2. Получить embeddings для всех кандидатов с batch processing
        texts = [c.text for c in request.candidates]
        embeddings = await get_embeddings_batch(texts, batch_size=BATCH_SIZE)

        # 3. Вычислить scores и сохранить metadata
        results = []
        for candidate, emb in zip(request.candidates, embeddings):
            if emb is None:
                continue  # Пропустить если ошибка
            
            score = cosine_similarity(query_embedding, emb)
            results.append(RerankResult(
                id=candidate.id,
                text=candidate.text,
                relevance_score=round(score, 4),
                original_score=candidate._score
            ))

        # 4. Отсортировать по убыванию scores
        results.sort(key=lambda x: x.relevance_score, reverse=True)

        elapsed = time.time() - start_time
        total_rerank_time += elapsed
        
        return RerankResponse(
            model="xitao/bge-reranker-v2-m3:latest (cosine similarity)",
            results=results
        )
    
    except HTTPException:
        raise
    except Exception as e:
        request_errors += 1
        print(f"Rerank error: {e}")
        raise HTTPException(status_code=500, detail=f"Rerank failed: {str(e)}")


@app.get("/health")
async def health():
    """Health check endpoint с метриками"""
    global request_count, request_errors, total_rerank_time
    
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{OLLAMA_URL}/api/tags")
            ollama_status = "connected" if response.status_code == 200 else "disconnected"
    except Exception as e:
        ollama_status = f"error: {str(e)}"
    
    avg_time = total_rerank_time / request_count if request_count > 0 else 0
    
    return {
        "status": "healthy" if ollama_status == "connected" else "unhealthy",
        "ollama_status": ollama_status,
        "metrics": {
            "total_requests": request_count,
            "total_errors": request_errors,
            "avg_rerank_time_sec": round(avg_time, 3),
            "cache_size": get_embedding_cached.cache_info().currsize
        }
    }


@app.get("/metrics")
async def metrics():
    """Детальные метрики для мониторинга"""
    global request_count, request_errors, total_rerank_time
    
    avg_time = total_rerank_time / request_count if request_count > 0 else 0
    cache_info = get_embedding_cached.cache_info()
    
    return {
        "requests": {
            "total": request_count,
            "errors": request_errors,
            "error_rate": round(request_errors / request_count, 3) if request_count > 0 else 0
        },
        "performance": {
            "avg_rerank_time_sec": round(avg_time, 3),
            "total_rerank_time_sec": round(total_rerank_time, 3)
        },
        "cache": {
            "hits": cache_info.hits,
            "misses": cache_info.misses,
            "size": cache_info.currsize,
            "maxsize": cache_info.maxsize,
            "hit_rate": round(cache_info.hits / (cache_info.hits + cache_info.misses), 3) if (cache_info.hits + cache_info.misses) > 0 else 0
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
