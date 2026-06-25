# Demand validation kit

Goal: find out whether people want a **tax-aware "what to trim & when" analyzer** *before*
spending money on lawyers or licensed market data. Spend $0, learn whether to keep going.

## Success bar (decide before you start)
Pick a number now so you don't move the goalposts later. A reasonable bar:

- **100+ waitlist emails** in 2–4 weeks from organic posts → real signal, worth building an MVP.
- **30–100** → lukewarm; niche may be too narrow or the message is off. Iterate the pitch.
- **< 30** → weak. Don't build yet.

Also watch *quality* signals: replies asking "when can I pay," "does it support Fidelity," DMs.
Those matter more than raw clicks.

---

## Step 1 — Waitlist form ✅ (done)
The landing page is wired to [Formspree](https://formspree.io) endpoint
`https://formspree.io/f/xojoarnn` (free tier = 50 submissions/mo) using the page's own
dependency-free AJAX handler.

- Submit a test email to yourself once the page is published to confirm it lands. The first
  real submission to a new Formspree form triggers a one-time email-confirmation step.

> Prefer no-code? Swap the whole form for a [Tally](https://tally.so) or Google Form embed.
> Want analytics? Add a free [Plausible](https://plausible.io) or
> [Simple Analytics](https://simpleanalytics.com) snippet to see visits → signup conversion.

## Step 2 — Publish the page (5 min, free)
You already deploy to GitHub Pages. Cheapest options:

- **GitHub Pages:** push `landing/` and enable Pages, OR
- **Netlify / Cloudflare Pages:** drag-and-drop the `landing` folder for an instant URL + free SSL.

Optional: a $10/yr domain (e.g. trimsmart.app) makes the Reddit/forum posts look legit.

## Step 3 — Post to communities (the actual experiment)
Use the drafts in `posts.md`. **Read each community's self-promotion rules first** — most ban
naked link drops. The framing below leads with a *question/insight*, not a pitch, which is what
gets through. Space posts a few days apart; don't blast all at once.

Targets, best first:
- r/dividends, r/Bogleheads, r/investing (check rules; some require a "no self-promo" workaround
  like posting the insight and linking only in a comment when asked)
- Bogleheads.org forum (older, tax-savvy, high-intent audience)
- r/tax around Nov–Dec (tax-loss-harvesting season = peak interest)
- Indie maker spots: r/SideProject, Hacker News "Show HN", Indie Hackers

## Step 4 — Talk to 5 people
The waitlist tells you *how many*; conversations tell you *why* and *what to charge*.
DM or email 5 signups: "What do you use today? What's annoying about it? Would you pay $X/mo?"
Five 10-minute chats will teach you more than 500 signups.

---

## What you are NOT doing yet (on purpose)
- No real product, no brokerage linking, no licensed data, no lawyer. All of that comes *after*
  the waitlist clears your success bar. The landing page describes the vision; that's allowed.
- Don't collect anything but an email. No passwords, no portfolio uploads during validation.

## Naming note
"TrimSmart" is a placeholder. To rename, find-and-replace `TrimSmart` across `index.html`,
`posts.md`, and the `<title>`. Check the name isn't taken (domain + trademark) before you commit.
