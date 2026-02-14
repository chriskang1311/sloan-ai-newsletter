"""
tools/generate_newsletter.py

Uses Claude to categorize articles, write newsletter copy, and build the HTML email.

Four Claude calls:
  1. categorize_and_select_articles — assigns articles to 'gossip' or 'builders' sections
  2. generate_section_stories (x2) — writes per-story content in the new format per section
  3. generate_opening_and_subject — writes subject line, hook opening, and story teasers

Then build_html_email assembles the full HTML email string.

Usage:
  from tools.generate_newsletter import generate_newsletter
  html, subject = generate_newsletter(
      anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
      articles=articles,
  )

Standalone test:
  python tools/generate_newsletter.py
"""

import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import TypedDict


def _load_logo_base64(logo_path: str = "ai_club_logo.jpg") -> str:
    """Loads the logo file and returns a base64 data URI for inline embedding."""
    try:
        with open(logo_path, "rb") as f:
            data = base64.b64encode(f.read()).decode("utf-8")
        ext = logo_path.rsplit(".", 1)[-1].lower()
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        return f"data:{mime};base64,{data}"
    except FileNotFoundError:
        return ""  # Falls back gracefully — no logo shown


CLAUDE_MODEL = "claude-sonnet-4-5-20250929"

NEWSLETTER_PERSONA = """You are the sole author of "AI News for Sloanies," a weekly newsletter for MBA students at MIT Sloan School of Management.

Your mandate:
- Your audience is highly educated but not technically deep on AI. They want to sound smart, advance their careers, and impress colleagues — not become engineers.
- Be highly self-aware about this motivation. Lean into it. Write as if you're in on the joke with the reader: "Here's what you need to know so you can hold your own at cocktail hour."
- Write in a single, consistent author voice: informed, witty, slightly tongue-in-cheek, but never condescending or dumbed-down.
- Synthesize and rewrite — never summarize or paraphrase verbatim. Produce fresh, cohesive prose that makes the reader feel they're getting insider commentary, not a news recap.
- Every piece of analysis must have a clear "so what" for an MBA student: how does this affect strategy, hiring, investment, or career positioning?
- Professional but entertaining. High readability is the goal — these students are busy and easily bored.
- Your readers are first-year Sloan MBAs currently taking six core courses: Competitive Strategy (15.900), Organizational Processes (15.311), Communications (15.280), Data for Management Decisions (15.060), Economics (15.010), and Financial Accounting (15.515). When an AI story genuinely mirrors a class framework, a well-placed wink lands perfectly — e.g., "your OP professor would call this a classic three-lens problem" or "Porter would file this under bargaining power." Never force it. One callback per story max, only when the fit is real."""


class Story(TypedDict):
    headline: str        # Conversational, engaging headline
    summary: str         # One sentence
    deets: list          # 3-4 bullet strings
    sound_smart: str     # 2-3 sentence strategic take
    url: str             # Source URL
    title: str           # Original article title (for attribution)
    source: str          # Publication name


class NewsletterSection(TypedDict):
    stories: list        # List of Story dicts


class NewsletterContent(TypedDict):
    subject_line: str
    greeting_finish: str     # Finish to "Hello Sloanies! ..."
    story_teasers: list      # ["OpenAI", "Airbnb AI", "Cohere IPO"] — top 3 across both sections
    gossip_section: NewsletterSection
    builders_section: NewsletterSection
    monday_date: str         # "February 9"
    sources: list            # [{title, url, source}] — all articles used


def _get_monday_date() -> str:
    """Returns the most recent Monday's date as a human-readable string."""
    today = datetime.now()
    days_since_monday = today.weekday()  # 0=Monday, 6=Sunday
    monday = today - timedelta(days=days_since_monday)
    return monday.strftime("%B %d")


def _call_claude_with_retry(client, system: str, user: str, max_retries: int = 3) -> str:
    """Calls Claude and returns the text response with exponential backoff on rate limits."""
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=3000,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return response.content[0].text
        except Exception as e:
            err = str(e)
            if "529" in err or "rate" in err.lower() or "overloaded" in err.lower():
                wait = 2 ** attempt
                print(f"  Claude rate limit. Waiting {wait}s before retry {attempt+1}/{max_retries}...", flush=True)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Claude API failed after {max_retries} retries.")


def _parse_json_response(raw: str, retry_prompt: str, client, system: str) -> dict:
    """Parses Claude's JSON. Strips code fences. Retries once on parse failure."""
    def _extract(text: str) -> dict:
        cleaned = re.sub(r"```(?:json)?\n?", "", text).strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            return json.loads(match.group())
        return json.loads(cleaned)

    try:
        return _extract(raw)
    except (json.JSONDecodeError, ValueError):
        retry = (
            f"{retry_prompt}\n\n"
            "IMPORTANT: Your previous response could not be parsed as JSON. "
            "Return ONLY valid JSON with no explanation, no markdown, no code fences. "
            "Just the raw JSON object."
        )
        raw2 = _call_claude_with_retry(client, system, retry)
        return _extract(raw2)


def _format_articles_for_prompt(articles: list) -> str:
    """Formats article list as numbered text for Claude prompts."""
    lines = []
    for i, a in enumerate(articles):
        lines.append(
            f"{i}. Title: {a['title']}\n"
            f"   Source: {a['source']} | Date: {a.get('published_at', '')[:10]}\n"
            f"   Description: {a.get('description', '')}\n"
            f"   URL: {a['url']}"
        )
    return "\n\n".join(lines)


def categorize_and_select_articles(client, articles: list) -> tuple:
    """
    Assigns articles to gossip vs builders sections.
    Returns (gossip_articles, builders_articles).
    """
    articles_text = _format_articles_for_prompt(articles)
    n = len(articles)

    user_prompt = f"""Here are {n} recent AI news articles (numbered 0 to {n-1}).
Select the best articles for two newsletter sections:

1. "AI Gossip: Hot Takes & Hallucinations" — Big tech company moves, executive drama,
   regulatory news, model releases from OpenAI/Google/Meta/Anthropic/Microsoft/Nvidia.
   Think: what MBAs will gossip about at networking events.

2. "AI Builders: Startups You'll Pretend You Already Knew About" — Startup funding rounds,
   new AI tools, product launches, open-source releases, developer ecosystem news.
   Think: what founders and VCs are actually building this week.

Rules:
- Select exactly 3 articles per section (the 3 strongest, most MBA-relevant)
- Each article can only appear in ONE section
- Skip articles that are not clearly about AI/tech — no sports, entertainment, or off-topic pieces
- Prioritize recency and business relevance for an MBA audience

Return JSON in this exact format:
{{
  "gossip_indices": [0, 3, 7],
  "builders_indices": [1, 4, 5]
}}

Articles:
{articles_text}"""

    raw = _call_claude_with_retry(client, NEWSLETTER_PERSONA, user_prompt)
    parsed = _parse_json_response(raw, user_prompt, client, NEWSLETTER_PERSONA)

    gossip_indices = parsed.get("gossip_indices", [])
    builders_indices = parsed.get("builders_indices", [])

    gossip_articles = [articles[i] for i in gossip_indices if 0 <= i < len(articles)]
    builders_articles = [articles[i] for i in builders_indices if 0 <= i < len(articles)]

    if len(gossip_articles) < 2:
        print("WARNING: Fewer than 2 gossip articles selected. Using first 3 as fallback.", flush=True)
        gossip_articles = articles[:3]
    if len(builders_articles) < 2:
        print("WARNING: Fewer than 2 builders articles selected. Using next 3 as fallback.", flush=True)
        builders_articles = articles[3:6]

    return gossip_articles, builders_articles


def generate_section_stories(client, section_name: str, articles: list) -> NewsletterSection:
    """
    Generates per-story content for one section.
    Each story: headline, summary, The Deets (3-4 bullets), How you can sound smart.
    """
    articles_text = _format_articles_for_prompt(articles)

    user_prompt = f"""You are writing the "{section_name}" section of this week's AI News for Sloanies newsletter.

You have {len(articles)} articles:
{articles_text}

For EACH article, write the following — in the voice of the newsletter persona (self-aware, witty, professional, MBA-focused):

1. headline: A conversational, engaging headline that conveys the main point. Not a news wire headline — write it like a smart friend summarizing it for you at dinner.

2. summary: Exactly ONE short sentence — 10 words or fewer. The single sharpest point of the story. No jargon, no filler.

3. deets: Exactly 3 bullet points — no more, no fewer. The 3 most essential details a Sloanie needs. Include numbers, names, and strategic context. Start each bullet with a relevant emoji. Keep each bullet to one concise sentence.

4. sound_smart: Exactly 1-2 sentences of strategic analysis an MBA could drop in conversation or an interview. Be specific — name companies, numbers, implications. No fluff. Lean into the self-aware tone.
   SPARINGLY — across all {len(articles)} stories in this section, include a Sloan course callback in at most 1 story (choose only the single best fit, and only if it's genuinely clever — not forced). Reference options:
   • AI pricing tiers / freemium → Econ 15.010: versioning, consumer self-selection (Keurig case)
   • AI company losses / burn rate → Acct 15.515: statement of cash flows, adjusted EBITDA
   • Big tech antitrust / market power → Econ: game theory, oligopoly; Strat 15.900: Five Forces
   • AI org restructuring / culture → OP 15.311: Three Lenses (structural, political, cultural)
   • AI talent wars → Econ / Strat: game theory, prisoner's dilemma on hiring pacts
   • Model hallucinations / AI output quality → DMD 15.060: "discerning consumers of AI output"
   • AI bias / fairness → DMD: Fairbank algorithmic fairness case
   • Network effects / platform moats → Strat: platform strategy, dynamic moats; Econ: network effects
   • AI layoffs / workforce reskilling → OP: AT&T reskilling case
   • Startup funding / value creation → Strat: value creation vs. value capture (Brandenburger & Stuart)
   • CEO drama / board fights / governance → OP: political lens, coalition building
   • AI earnings / revenue recognition → Acct: accrual vs. cash, revenue recognition (Spartan Race case)
   • Data privacy / AI energy use → Econ: externalities
   • Product launch / CEO keynote → Comms 15.280: SHIELD framework, know your audience
   • GPU shortage / chip supply chain → Strat: bargaining power (Tempur Sealy case)
   • Benchmark claims / model accuracy → DMD: evaluating classification models, false positives

Return a JSON object with a "stories" array:
{{
  "stories": [
    {{
      "headline": "...",
      "summary": "...",
      "deets": ["🔹 Detail one...", "🔹 Detail two...", "🔹 Detail three..."],
      "sound_smart": "...",
      "url": "exact URL from the article data above",
      "title": "exact original title from the article data above",
      "source": "exact source name from the article data above"
    }}
  ]
}}

Write all {len(articles)} stories. Return ONLY the JSON."""

    raw = _call_claude_with_retry(client, NEWSLETTER_PERSONA, user_prompt)
    parsed = _parse_json_response(raw, user_prompt, client, NEWSLETTER_PERSONA)

    stories = parsed.get("stories", [])
    if not stories:
        raise ValueError(f"Claude returned no stories for section '{section_name}'")

    return NewsletterSection(stories=stories)


def generate_opening_and_subject(
    client,
    gossip_articles: list,
    builders_articles: list,
    monday_date: str,
) -> tuple:
    """
    Generates the subject line, "Hello Sloanies!" greeting, and top story teasers.
    Returns (subject_line, greeting_finish, story_teasers).
    """
    all_titles = (
        [f"[GOSSIP] {a['title']}" for a in gossip_articles] +
        [f"[BUILDERS] {a['title']}" for a in builders_articles]
    )
    titles_text = "\n".join(f"- {t}" for t in all_titles)

    user_prompt = f"""You are writing the opening elements for AI News for Sloanies, Week of {monday_date}.

This week's stories:
{titles_text}

Write three things:

1. subject_line: One compelling email subject line. Must start with "Sloan AI Club Weekly Newsletter |" followed by a punchy, topic-specific hook tied to the week's top headline. Example format: "Sloan AI Club Weekly Newsletter | xAI's Meltdown & What It Means for You". Keep the hook under 50 characters.

2. greeting_finish: The witty finish to the sentence "Hello Sloanies! ". This should riff on the week's top story. Keep it to one clause or sentence. Punchy, self-aware, and relevant to the MBA context (e.g., "...your AI overlords are having a rough week, and honestly, same.").

3. story_teasers: A list of exactly 3 items — one-to-two-word conversational indicators for the top 3 stories this week (pick the 3 most interesting across both sections). These appear in: "In this week's newsletter, we cover [X], [Y], and [Z]." Examples: "OpenAI's drama", "Airbnb's pivot", "Cohere's IPO".

Return JSON:
{{
  "subject_line": "...",
  "greeting_finish": "...",
  "story_teasers": ["...", "...", "..."]
}}"""

    raw = _call_claude_with_retry(client, NEWSLETTER_PERSONA, user_prompt)
    parsed = _parse_json_response(raw, user_prompt, client, NEWSLETTER_PERSONA)

    subject_line = parsed.get("subject_line", f"AI News for Sloanies | Week of {monday_date}")
    greeting_finish = parsed.get("greeting_finish", "this week in AI did not disappoint.")
    story_teasers = parsed.get("story_teasers", ["AI news", "startup moves", "big tech"])

    return subject_line, greeting_finish, story_teasers


def build_html_email(content: NewsletterContent, logo_data_uri: str = "") -> str:
    """
    Assembles the full HTML email from newsletter content.
    Table-based layout with inline CSS for Gmail compatibility.
    logo_data_uri: base64 data URI string, or empty string to show no logo.
    """

    def _render_story(story: dict, accent_color: str) -> str:
        deets_items = "".join(
            f'<li style="margin-bottom: 10px; line-height: 1.65; color: #333; font-family: Arial, Helvetica, sans-serif; font-size: 14px;">{d}</li>'
            for d in story.get("deets", [])
        )
        return f"""
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-bottom: 28px;">
          <tr>
            <td style="border-left: 3px solid {accent_color}; padding-left: 14px;">
              <!-- Story headline -->
              <h3 style="margin: 0 0 6px 0; font-size: 17px; font-weight: bold; color: #1a1a2e; font-family: Arial, Helvetica, sans-serif; line-height: 1.4;">
                <a href="{story.get('url', '#')}" style="color: #1a1a2e; text-decoration: none;">{story.get('headline', '')}</a>
              </h3>
              <!-- One-sentence summary -->
              <p style="margin: 0 0 12px 0; font-size: 14px; color: #555; font-family: Arial, Helvetica, sans-serif; line-height: 1.6; font-style: italic;">
                {story.get('summary', '')}
              </p>
              <!-- The Deets -->
              <p style="margin: 0 0 6px 0; font-size: 13px; font-weight: bold; color: #1a1a2e; font-family: Arial, Helvetica, sans-serif; text-transform: uppercase; letter-spacing: 0.5px;">
                The Deets:
              </p>
              <ul style="margin: 0 0 14px 0; padding-left: 18px;">
                {deets_items}
              </ul>
              <!-- How you can sound smart -->
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="background-color: #fff8e1; border-left: 3px solid #f9a825; padding: 12px 14px; border-radius: 3px;">
                    <p style="margin: 0 0 4px 0; font-size: 12px; font-weight: bold; color: #e65100; text-transform: uppercase; letter-spacing: 0.5px; font-family: Arial, Helvetica, sans-serif;">
                      &#9749; How you can sound smart talking about this:
                    </p>
                    <p style="margin: 0; font-size: 14px; line-height: 1.6; color: #4a3000; font-family: Arial, Helvetica, sans-serif;">
                      {story.get('sound_smart', '')}
                    </p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>"""

    def _render_section(title: str, subtitle: str, accent_color: str, section: NewsletterSection) -> str:
        stories_html = "".join(
            _render_story(s, accent_color) for s in section["stories"]
        )
        return f"""
        <!-- Section header -->
        <tr>
          <td style="padding: 0 30px 10px 30px;">
            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td style="border-left: 4px solid {accent_color}; padding: 10px 0 10px 16px;">
                  <h2 style="margin: 0 0 2px 0; font-size: 19px; color: #1a1a2e; font-family: Arial, Helvetica, sans-serif; font-weight: bold;">
                    {title}
                  </h2>
                  <p style="margin: 0; font-size: 12px; color: #888; font-family: Arial, Helvetica, sans-serif; font-style: italic;">
                    {subtitle}
                  </p>
                </td>
              </tr>
            </table>
          </td>
        </tr>
        <!-- Section stories -->
        <tr>
          <td style="padding: 8px 30px 20px 30px;">
            {stories_html}
          </td>
        </tr>
        <!-- Divider -->
        <tr>
          <td style="padding: 0 30px 24px 30px;">
            <hr style="border: none; border-top: 1px solid #e8e8e8; margin: 0;" />
          </td>
        </tr>"""

    # Build sources line
    sources_lines = []
    for s in content.get("sources", []):
        sources_lines.append(
            f'<a href="{s["url"]}" style="color: #888; font-size: 11px; font-family: Arial, Helvetica, sans-serif;">{s["title"]} ({s["source"]})</a>'
        )
    sources_html = " &bull; ".join(sources_lines) if sources_lines else ""

    # Teasers for opening
    teasers = content["story_teasers"]
    if len(teasers) >= 3:
        teasers_str = f"{teasers[0]}, {teasers[1]}, and {teasers[2]}"
    else:
        teasers_str = ", ".join(teasers)

    monday_date = content["monday_date"]

    gossip_html = _render_section(
        title="AI Gossip: Hot Takes &amp; Hallucinations",
        subtitle="The moves, the drama, and the billion-dollar bets you need to know about",
        accent_color="#2196F3",
        section=content["gossip_section"],
    )

    builders_html = _render_section(
        title="AI Builders: Startups You&#39;ll Pretend You Already Knew About",
        subtitle="Funding rounds, product launches, and the tools your future employer is probably already using",
        accent_color="#9C27B0",
        section=content["builders_section"],
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AI News for Sloanies: Week of {monday_date}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f0f2f5; font-family: Arial, Helvetica, sans-serif;">

  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color: #f0f2f5;">
    <tr>
      <td align="center" style="padding: 24px 16px;">

        <table width="600" cellpadding="0" cellspacing="0" border="0" style="max-width: 600px; width: 100%; background-color: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08);">

          <!-- HEADER -->
          <tr>
            <td style="background-color: #1a1a2e; padding: 28px 30px 20px 30px; text-align: center;">
              {f'<img src="{logo_data_uri}" alt="MIT Sloan AI Club" width="110" height="110" style="display: block; margin: 0 auto 12px auto; border-radius: 8px;" />' if logo_data_uri else ''}
              <h1 style="margin: 0 0 4px 0; font-size: 20px; font-weight: bold; color: #ffffff; font-family: Arial, Helvetica, sans-serif;">
                AI News for Sloanies
              </h1>
              <p style="margin: 0; font-size: 13px; color: #a0a8c8; font-family: Arial, Helvetica, sans-serif;">
                Week of {monday_date}
              </p>
            </td>
          </tr>

          <!-- OPENING HOOK -->
          <tr>
            <td style="background-color: #f7f8fc; padding: 20px 30px 18px 30px; border-bottom: 1px solid #e8e8e8;">
              <p style="margin: 0 0 8px 0; font-size: 15px; color: #333; font-family: Arial, Helvetica, sans-serif; line-height: 1.6;">
                Hello Sloanies! {content['greeting_finish']}
              </p>
              <p style="margin: 0; font-size: 14px; color: #666; font-family: Arial, Helvetica, sans-serif; line-height: 1.6;">
                In this week&#39;s newsletter, we cover <strong>{teasers_str}</strong>.
              </p>
            </td>
          </tr>

          <!-- SPACER -->
          <tr><td style="height: 24px;"></td></tr>

          {gossip_html}

          {builders_html}

          <!-- SOURCES -->
          <tr>
            <td style="padding: 0 30px 20px 30px;">
              <p style="margin: 0 0 6px 0; font-size: 11px; font-weight: bold; color: #aaa; font-family: Arial, Helvetica, sans-serif; text-transform: uppercase; letter-spacing: 0.5px;">
                Sources
              </p>
              <p style="margin: 0; line-height: 1.8;">
                {sources_html}
              </p>
            </td>
          </tr>

          <!-- FOOTER -->
          <tr>
            <td style="background-color: #1a1a2e; padding: 20px 30px; text-align: center;">
              <p style="margin: 0 0 6px 0; font-size: 13px; color: #a0a8c8; font-family: Arial, Helvetica, sans-serif;">
                <strong style="color: #ffffff;">AI News for Sloanies</strong> &bull; MIT Sloan School of Management
              </p>
              <p style="margin: 0; font-size: 11px; color: #4a4d6a; font-family: Arial, Helvetica, sans-serif;">
                Powered by Claude &bull; Delivered every Wednesday at 8am ET
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>

</body>
</html>"""

    return html


def generate_newsletter(
    anthropic_api_key: str,
    articles: list,
    logo_path: str = "ai_club_logo.jpg",
) -> tuple:
    """
    Main entry point. Orchestrates all Claude calls and builds the HTML email.
    Returns (html_string, subject_line).
    logo_path: path to the logo file (relative or absolute).
    """
    try:
        import anthropic
    except ImportError:
        raise ImportError("Run: pip install anthropic")

    client = anthropic.Anthropic(api_key=anthropic_api_key)
    monday_date = _get_monday_date()
    logo_data_uri = _load_logo_base64(logo_path)
    if logo_data_uri:
        print(f"  Logo loaded from {logo_path}", flush=True)
    else:
        print(f"  WARNING: Logo not found at '{logo_path}'. Header will show without logo.", flush=True)

    print("Categorizing articles with Claude...", flush=True)
    gossip_articles, builders_articles = categorize_and_select_articles(client, articles)
    print(f"  Gossip: {len(gossip_articles)} articles | Builders: {len(builders_articles)} articles", flush=True)

    print("Generating AI Gossip stories...", flush=True)
    gossip_section = generate_section_stories(client, "AI Gossip: Hot Takes & Hallucinations", gossip_articles)

    print("Generating AI Builders stories...", flush=True)
    builders_section = generate_section_stories(client, "AI Builders: Startups You'll Pretend You Already Knew About", builders_articles)

    print("Generating subject line and opening...", flush=True)
    subject_line, greeting_finish, story_teasers = generate_opening_and_subject(
        client, gossip_articles, builders_articles, monday_date
    )

    # Collect sources from all selected articles
    all_articles = gossip_articles + builders_articles
    sources = [{"title": a["title"], "url": a["url"], "source": a["source"]} for a in all_articles]

    content = NewsletterContent(
        subject_line=subject_line,
        greeting_finish=greeting_finish,
        story_teasers=story_teasers,
        gossip_section=gossip_section,
        builders_section=builders_section,
        monday_date=monday_date,
        sources=sources,
    )

    print("Building HTML email...", flush=True)
    html = build_html_email(content, logo_data_uri=logo_data_uri)

    return html, subject_line


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    required = ["NEWSAPI_KEY", "ANTHROPIC_API_KEY"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: Missing environment variables: {', '.join(missing)}")
        sys.exit(1)

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from tools.scrape_news import scrape_news

    print("=== Scraping news ===")
    articles = scrape_news(newsapi_key=os.environ["NEWSAPI_KEY"])

    print("\n=== Generating newsletter ===")
    html, subject = generate_newsletter(
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        articles=articles,
    )

    os.makedirs(".tmp", exist_ok=True)
    with open(".tmp/newsletter_preview.html", "w") as f:
        f.write(html)

    print(f"\nSubject line: {subject}")
    print(f"Newsletter HTML written to .tmp/newsletter_preview.html")
    print("Open it in a browser to preview.")
