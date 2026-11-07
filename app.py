"""A general web research agent powered by Sodeom and LangChain."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from langchain.agents import create_agent
from langchain.tools import tool
from langchain_openai import ChatOpenAI
from markdownify import markdownify
from pydantic import BaseModel, Field


SYSTEM_PROMPT = """\
You are a general-purpose web research assistant. Answer the user's question
using the supplied search results and fetched webpage content. Use the fetched
content as evidence, distinguish facts from opinions, and never invent facts or
URLs. Include a short Sources list with the URLs actually consulted.
"""


class FactCheck(BaseModel):
    """Structured verdict returned by the fact-checking agent."""

    verdict: Literal["true", "false"] = Field(
        description="Whether the draft is sufficiently supported and accurate."
    )
    feedback: str = Field(
        description="Specific corrections required when the draft is false."
    )


@tool
def search_web(query: str) -> str:
    """Search the web for current information on any topic."""
    base_url = os.getenv("SODEOM_BASE_URL", "https://sodeom.com").rstrip("/")
    params = urlencode({"q": query, "page": 1})
    try:
        request = Request(
            f"{base_url}/api/search?{params}",
            headers={"User-Agent": "sodeom-langchain-search-agent/1.0"},
        )
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except Exception as exc:
        raise RuntimeError(f"Sodeom web search failed: {exc}") from exc

    results = payload.get("results", [])
    if not results:
        return f"No results found for: {query}"

    return "\n\n".join(
        f"Title: {item.get('title', '')}\n"
        f"URL: {item.get('link', '')}\n"
        f"Summary: {item.get('description', '')}"
        for item in results
    )


@tool
def web_fetch(url: str) -> str:
    """Fetch a webpage with curl-like HTTP behavior and convert its HTML to Markdown."""
    if not url.startswith(("http://", "https://")):
        raise ValueError("web_fetch only accepts http:// or https:// URLs")

    try:
        request = Request(
            url,
            headers={"User-Agent": "sodeom-langchain-fetch-agent/1.0"},
        )
        with urlopen(request, timeout=20) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                body = response.read(120_000).decode("utf-8", errors="replace")
                return f"Content-Type: {content_type}\n\n{body[:12000]}"
            html = response.read(500_000).decode("utf-8", errors="replace")
    except Exception as exc:
        raise RuntimeError(f"Webpage fetch failed for {url}: {exc}") from exc

    document = BeautifulSoup(html, "html.parser")
    for element in document(["script", "style", "noscript", "svg"]):
        element.decompose()
    markdown = markdownify(str(document), heading_style="ATX")
    return f"Source URL: {url}\n\n{markdown[:12000]}"


def build_agent() -> object:
    """Create the general search agent."""
    model = ChatOpenAI(
        base_url=os.getenv("SODEOM_OPENAI_BASE_URL", "https://sodeom.com/v1"),
        api_key=os.getenv("SODEOM_API_KEY", "sodeom"),
        model=os.getenv("SODEOM_MODEL", "beam"),
        temperature=0.2,
        max_tokens=3000,
    )
    return create_agent(model=model, tools=[], system_prompt=SYSTEM_PROMPT)


def collect_research(query: str) -> str:
    """Search and fetch the most relevant pages for a query."""
    search_results = search_web.invoke(query)
    urls = list(dict.fromkeys(re.findall(r"^URL: (https?://\S+)$", search_results, re.MULTILINE)))
    pages = [f"Search results:\n{search_results[:6000]}"]
    for url in urls[:3]:
        pages.append(web_fetch.invoke(url))
    return "\n\n---\n\n".join(pages)


def answer_query(
    query: str, output_path: Path | None = None, max_attempts: int = 3
) -> None:
    """Research, fact-check, and print or save the Markdown answer."""
    if not query.strip():
        raise ValueError("query must not be empty")
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    dossier = collect_research(query)
    agent = build_agent()
    model = ChatOpenAI(
        base_url=os.getenv("SODEOM_OPENAI_BASE_URL", "https://sodeom.com/v1"),
        api_key=os.getenv("SODEOM_API_KEY", "sodeom"),
        model=os.getenv("SODEOM_MODEL", "beam"),
        temperature=0,
    )
    fact_checker = model.with_structured_output(
        FactCheck,
        method="function_calling",
    )

    feedback = ""
    answer = ""
    verdict = "false"
    for _ in range(max_attempts):
        retry_context = (
            f"\nPrevious fact-check feedback:\n{feedback}\n"
            if feedback
            else ""
        )
        result = agent.invoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            f"Question: {query}\n"
                            f"{retry_context}\n"
                            f"Research dossier:\n{dossier}"
                        ),
                    }
                ]
            }
        )
        messages = result.get("messages", [])
        if not messages:
            raise RuntimeError("The answer agent returned no messages.")
        answer = messages[-1].content
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("The answer agent returned an empty answer.")

        check = fact_checker.invoke(
            [
                (
                    "system",
                    "You are a strict fact-checking agent. Check every material "
                    "claim in the draft against the research dossier. Mark it true "
                    "only when it is supported and accurate. Give specific fixes "
                    "for unsupported, contradictory, or overstated claims.",
                ),
                (
                    "human",
                    f"Question: {query}\n"
                    f"Research dossier:\n{dossier}\n\n"
                    f"Draft answer:\n{answer}",
                ),
            ]
        )
        verdict, feedback = check.verdict, check.feedback
        if verdict == "true":
            break

    if verdict != "true":
        answer += (
            "\n\n> Warning: the fact checker found unresolved issues after "
            f"{max_attempts} attempts.\n"
        )

    answer = answer.strip() + "\n"
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(answer, encoding="utf-8")
        print(f"Answer written to {output_path}")
    else:
        print(answer, end="")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search the web, fetch pages as Markdown, and answer a question."
    )
    parser.add_argument("query", help="The question or topic to research")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Optional Markdown output path (otherwise print to stdout)",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Maximum draft/fact-check attempts (default: 3)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    answer_query(args.query, args.output, args.max_attempts)