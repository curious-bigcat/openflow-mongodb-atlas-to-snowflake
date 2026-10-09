"""Continuously write inserts/updates/deletes to MongoDB Atlas (retail db) to exercise Openflow CDC.

Usage:
  export MONGO_USER=bsuresh            # any user with write access
  export MONGO_PASSWORD='...'
  python stream_to_mongo.py --interval 1 --batch 5
"""
import argparse
import os
import random
import time
from datetime import datetime, timezone

from pymongo import MongoClient

URI = os.getenv("MONGO_URI", "mongodb+srv://demo.mxicyu.mongodb.net/?authSource=admin")
NAMES = ["Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Heidi", "Ivan", "Judy"]
CITIES = ["Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide", "Auckland"]
TIERS = ["bronze", "silver", "gold", "platinum"]
ITEMS = ["shoe", "sock", "hat", "shirt", "jacket", "bag", "watch"]


def now():
    return datetime.now(timezone.utc)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=1.0, help="seconds between batches")
    p.add_argument("--batch", type=int, default=5, help="operations per batch")
    p.add_argument("--max-batches", type=int, default=0, help="0 = run forever")
    args = p.parse_args()

    client = MongoClient(URI, username=os.environ["MONGO_USER"], password=os.environ["MONGO_PASSWORD"])
    db = client["retail"]
    stats = {"insert": 0, "update": 0, "delete": 0}
    n = 0
    try:
        while not args.max_batches or n < args.max_batches:
            for _ in range(args.batch):
                op = random.choices(["insert", "update", "delete"], weights=[70, 25, 5])[0]
                if op == "insert":
                    name = random.choice(NAMES)
                    db.customers.insert_one({"name": name, "city": random.choice(CITIES),
                                             "tier": random.choice(TIERS), "created_at": now()})
                    db.orders.insert_one({"customer": name, "amount": round(random.uniform(5, 500), 2),
                                          "items": random.sample(ITEMS, k=random.randint(1, 3)), "ts": now()})
                elif op == "update":
                    db.customers.update_one({"name": random.choice(NAMES)},
                                            {"$set": {"tier": random.choice(TIERS), "updated_at": now()}})
                else:
                    doc = db.orders.find_one(sort=[("_id", 1)])
                    if doc:
                        db.orders.delete_one({"_id": doc["_id"]})
                stats[op] += 1
            n += 1
            print(f"{now():%H:%M:%S} batch {n}  totals {stats}  "
                  f"customers={db.customers.estimated_document_count()} orders={db.orders.estimated_document_count()}",
                  flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("stopped", stats)
    finally:
        client.close()


if __name__ == "__main__":
    main()
