"""
The agent app's router — /agent-api/v1/*.

A separate contract layer, following the app/lizo/ precedent: distinct routes and
schemas over shared domain logic, never URL-prefix branching inside shared handlers.
Three prefixes now exist and they mean different things:

    /api/*            web panel — JWT bearer or session cookie
    /client-api/v1/*  ISV clients — X-API-Key
    /agent-api/v1/*   the agent app — opaque session token + X-Device-Id

This module is the seam. The inbox endpoints (threads, messages, media) attach here as
they are built; nothing above them has to move.
"""
from fastapi import APIRouter

from app.agent import auth, broadcasts, messages, phonebooks, templates, threads

router = APIRouter(prefix="/agent-api/v1")

router.include_router(auth.router)
router.include_router(templates.router)
router.include_router(phonebooks.router)
router.include_router(threads.router)
router.include_router(messages.router)
router.include_router(broadcasts.router)

# Attaching later, as each is built:
#   router.include_router(media.router)
