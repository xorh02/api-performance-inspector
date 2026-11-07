"""An ordinary FastAPI service that uses API Performance Inspector as an installed package.

Only the block at the bottom marked "Performance instrumentation" relates to the
integration. Run it with:  uvicorn app:app
"""

import asyncio
import os
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from api_performance_inspector import PerformanceInspector


class Item(BaseModel):
    sku: str
    name: str
    quantity: int = Field(ge=0)


class Inventory:
    """Stands in for a real database client."""

    def __init__(self) -> None:
        self.items = {
            "A-1": Item(sku="A-1", name="Hex bolt", quantity=120),
            "B-7": Item(sku="B-7", name="Wing nut", quantity=40),
        }

    async def get(self, sku: str) -> Item | None:
        await asyncio.sleep(0.005)
        return self.items.get(sku)

    async def search(self, text: str) -> list[Item]:
        await asyncio.sleep(0.15)  # an unindexed scan: deliberately slow
        return [i for i in self.items.values() if text.lower() in i.name.lower()]


inventory = Inventory()


def get_inventory() -> Inventory:
    return inventory


InventoryDep = Annotated[Inventory, Depends(get_inventory)]

app = FastAPI(title="Inventory service")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/items/{sku}")
async def read_item(sku: str, db: InventoryDep) -> Item:
    item = await db.get(sku)
    if item is None:
        raise HTTPException(status_code=404, detail=f"unknown sku {sku}")
    return item


@app.put("/items/{sku}")
async def update_item(sku: str, item: Item, db: InventoryDep) -> Item:
    db.items[sku] = item
    return item


@app.get("/search")
async def search(q: str, db: InventoryDep) -> list[Item]:
    return await db.search(q)


# Performance instrumentation. Set INVENTORY_PROFILING=0 to run without it.
if os.environ.get("INVENTORY_PROFILING", "1") != "0":
    inspector = PerformanceInspector(
        storage="sqlite",
        database_path="inventory_metrics.db",
        slow_request_threshold_ms=100,
        exclude_paths=["/healthz"],
    )
    inspector.instrument(app)
