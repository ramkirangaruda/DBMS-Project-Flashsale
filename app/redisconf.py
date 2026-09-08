"""
One place to build the Redis client, shared by app/main.py and the seed
scripts that also need to poke Redis directly (clearing a stock counter,
mostly).

Locally this is docker-compose's Redis on host:port. On Vercel there is no
docker-compose, so a managed Redis (Upstash, mainly -- it speaks the plain
Redis protocol over TLS, not just its REST API) is configured with a single
REDIS_URL instead: something like

    rediss://default:<token>@usw1-example-12345.upstash.io:6379

REDIS_URL wins outright when set, the same way DATABASE_URL wins over the
piecemeal Postgres vars in app/database.py -- see that file's docstring for
the matching reasoning.
"""
import os

import redis


def make_redis_client(decode_responses=True):
    url = os.getenv("REDIS_URL")
    if url:
        return redis.Redis.from_url(url, decode_responses=decode_responses)
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6390")),  # matches docker-compose.yml (6390:6379)
        decode_responses=decode_responses,
    )


def is_tls(redis_client):
    """Whether `redis_client` was built with an SSL connection pool -- needed
    by app/queue.py to build its async twin against the same kind of
    connection (see the note in install() there)."""
    from redis.connection import SSLConnection

    return redis_client.connection_pool.connection_class is SSLConnection
