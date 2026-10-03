"""Write routes (issue #39; D13). Empty until #40/#41; rate-limited as a group."""

from fastapi import APIRouter, Depends

from services.api.deps import rate_limit

router = APIRouter(dependencies=[Depends(rate_limit)])
