# # # test_symbols.py

# # import asyncio
# # from sqlalchemy import select, func

# # from app.models import AsyncSessionLocal, Symbol


# # async def main():
# #     async with AsyncSessionLocal() as db:

# #         result = await db.execute(
# #             select(func.count(Symbol.id))
# #         )

# #         print("Symbol count:", result.scalar())

# #         result = await db.execute(
# #             select(Symbol).limit(10)
# #         )

# #         rows = result.scalars().all()

# #         print("\nFirst symbols:\n")

# #         for s in rows:
# #             print(
# #                 s.name,
# #                 s.kind,
# #                 s.file_path,
# #             )


# # asyncio.run(main())

# import asyncio

# from app.retrieval.budget import RetrievalBudget
# from app.retrieval.tools import search_symbols


# async def main():
#     budget = RetrievalBudget(max_tokens=1000)

#     result = await search_symbols(
#         workspace_id="ws_dde99fd433c0",
#         query="Task",
#         budget=budget,
#     )

#     print(result)

#     print("\nBudget:")
#     print(budget.snapshot())


# asyncio.run(main())


# import asyncio
# from pathlib import Path

# from app.retrieval.indexer import index_workspace

# WORKSPACE_ID = "ws_b62ae1d15b55"

# async def main():
#     stats = await index_workspace(
#         workspace_id=WORKSPACE_ID,
#         worktree_path=Path(f"data/repos/{WORKSPACE_ID}")
#     )

#     print(stats)

# asyncio.run(main())


# import asyncio

# from app.retrieval.budget import RetrievalBudget
# from app.retrieval.tools import search_symbols


# async def main():
#     budget = RetrievalBudget(max_tokens=1000)

#     result = await search_symbols(
#         workspace_id="ws_b62ae1d15b55",
#         query="Task",
#         budget=budget,
#     )

#     print(result)
#     print()
#     print(budget.snapshot())


# asyncio.run(main())


# import asyncio
# from pathlib import Path

# from app.retrieval.budget import RetrievalBudget
# from app.retrieval.tools import definition


# async def main():
#     budget = RetrievalBudget(max_tokens=5000)

#     result = await definition(
#         workspace_id="ws_b62ae1d15b55",
#         worktree_path=Path("data/repos/ws_b62ae1d15b55"),
#         symbol_name="TaskState",
#         budget=budget,
#     )

#     print(result)

# asyncio.run(main())


# from pathlib import Path

# from app.retrieval.budget import RetrievalBudget
# from app.retrieval.tools import references

# budget = RetrievalBudget(max_tokens=100000)

# result = references(
#     symbol_name="TaskState",
#     worktree_path=Path(
#         "data/repos/ws_b62ae1d15b55"
#     ),
#     budget=budget,
# )

# print(result)
# print()
# print(budget.snapshot())


# from pathlib import Path

# from app.retrieval.budget import RetrievalBudget
# from app.retrieval.tools import list_dir

# budget = RetrievalBudget(max_tokens=1000)

# result = list_dir(
#     worktree_path=Path(
#         "data/repos/ws_b62ae1d15b55"
#     ),
#     rel_path="app",
#     budget=budget,
# )

# print(result)


from pathlib import Path

from app.retrieval.budget import RetrievalBudget
from app.retrieval.tools import structural_grep

budget = RetrievalBudget(max_tokens=100000)

# result = structural_grep(
#     worktree_path=Path("data/repos/ws_b62ae1d15b55"),
#     pattern="TaskState",
#     budget=budget,
# )
# result = structural_grep(
#     worktree_path=Path("data/repos/ws_b62ae1d15b55"),
#     pattern="BUDGET_EXHAUSTED",
#     budget=budget,
# )
result = structural_grep(
    worktree_path=Path("data/repos/ws_b62ae1d15b55"),
    pattern="TaskState.CANCELLED",
    budget=budget,
)

print(result)
print()
print(budget.snapshot())