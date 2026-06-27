"""
test_metadata_fetch.py

Interactive test for substeps 1 + 2 of the AniList metadata pipeline.

Run with: py test_metadata_fetch.py
Then type any manga title and press Enter. Type 'quit' to exit.

For each search, this prints the AniList candidates sorted by match
score (best first), showing the score next to the title rather than
the AniList ID, since the ID isn't useful for eyeballing whether the
matching logic is doing something sensible.
"""

import asyncio
from metadata_fetch import search_anilist_manga, score_all_candidates


def print_results(query: str, scored_results: list[dict]) -> None:
    if not scored_results:
        print("  (no results found)")
        return

    for r in scored_results:
        # Prefer english title for display if present, otherwise romaji.
        display_title = r["title_english"] or r["title_romaji"] or "(no title)"
        print(f"  [{r['match_score']:.2f}]  {display_title}")
        # Show romaji underneath too if it differs from what we displayed,
        # since that's often the title that actually matches folder names.
        if r["title_romaji"] and r["title_romaji"] != display_title:
            print(f"          ({r['title_romaji']})")


async def main():
    print("AniList match tester. Type a title to search, or 'quit' to exit.\n")
    while True:
        query = input("Search title: ").strip()
        if not query:
            continue
        if query.lower() in ("quit", "exit"):
            break

        try:
            raw_results = await search_anilist_manga(query)
        except Exception as e:
            print(f"  Error: {e}")
            continue

        scored = score_all_candidates(query, raw_results)
        print_results(query, scored)
        print()


if __name__ == "__main__":
    asyncio.run(main())
