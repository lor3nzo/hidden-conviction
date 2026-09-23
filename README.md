# Hidden Conviction

![Python](https://img.shields.io/badge/Python-3.13%2B-blue)
![Cloudflare Workers](https://img.shields.io/badge/Cloudflare-Workers-orange)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Version](https://img.shields.io/badge/version-0.14.0-blue)
[![CI](https://github.com/lor3nzo/hidden-conviction/actions/workflows/ci.yml/badge.svg)](https://github.com/lor3nzo/hidden-conviction/actions/workflows/ci.yml)

**An open-source SEC filing intelligence engine for detecting meaningful changes in insider, beneficial-owner, institutional, and corporate activity.**

Hidden Conviction ingests public SEC EDGAR filings, structures the underlying evidence, and converts it into auditable research signals.

It currently analyzes:

- Form 4 insider transactions
- Schedule 13D / 13G beneficial ownership filings
- Form 8-K corporate events
- Form 13F institutional holdings

Live application:

**https://hidden-conviction.aotwone.workers.dev/**

> Hidden Conviction is research software. It does not provide investment advice.

---

## Why Hidden Conviction Exists

SEC filings contain valuable information, but the useful signals are scattered across different filing types, formats, reporting periods, and issuers.

A single filing often means very little in isolation.

The more interesting question is whether multiple independent sources of evidence are beginning to converge.

Hidden Conviction is designed to:

- collect relevant SEC filings automatically
- normalize them into structured data
- preserve the underlying evidence
- identify changes that may deserve further research
- combine independent signals without hiding their provenance
- make the resulting analysis inspectable and reproducible

The goal is not to predict stock prices.

The goal is to make public SEC information easier to research systematically.

---

## Signal Families

### Insider Activity

Form 4 filings are analyzed for insider purchases and related activity.

The system preserves filing-level evidence and distinguishes relevant transactions from activity that should not contribute to a conviction signal.

### Beneficial Ownership

Schedule 13D and 13G filings are used to identify significant ownership positions and changes in ownership.

Amendments are treated as first-class filings rather than opaque duplicates.

### Corporate Events

Form 8-K filings are parsed for potentially material corporate events.

The pipeline uses filing context and event-specific rules rather than relying solely on keyword matches.

### Institutional Holdings

13F filings are processed as delayed institutional confirmation data.

Institutional observations are evaluated quarter-over-quarter and require predecessor data when appropriate.

13F signals are deliberately constrained so that institutional holdings alone cannot create conviction.

---

## Hidden Conviction Score

Hidden Conviction maintains a composite research score called the **Hidden Conviction Score (HCS)**.

HCS combines independent evidence families such as:

- insider activity
- insider clustering
- beneficial ownership
- institutional confirmation
- selected 8-K events
- cross-signal convergence
- data-quality and eligibility controls

The score is designed to remain auditable.

Supporting evidence is stored separately so that a score can be traced back to the filings and observations that produced it.

The scoring model should be treated as a research framework, not as a forecast of future investment returns.

---

## Architecture

Hidden Conviction runs primarily on Cloudflare infrastructure.

```text
                         SEC EDGAR
                            |
                            v
                +-------------------------+
                | hidden-conviction-ingest|
                | Python / FastAPI        |
                |                         |
                | SEC discovery           |
                | Filing processing       |
                | Scoring                 |
                | Cron                    |
                | Operational APIs        |
                +------------+------------+
                             |
                    Cloudflare Queues
                       /           \
                      v             v
          General filing queue     13F queue
                                   |
                                   v
                         +--------------------+
                         | hidden-conviction- |
                         | 13f                |
                         | Python Worker      |
                         +---------+----------+
                                   |
                                   v
                    +--------------------------+
                    | Cloudflare D1            |
                    | hidden-conviction-db     |
                    +------------+-------------+
                                 |
                  +--------------+--------------+
                  |                             |
                  v                             v
        hidden-conviction                Public APIs
        JavaScript edge Worker
                  |
                  v
              Browser
