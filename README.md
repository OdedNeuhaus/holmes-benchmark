# HolmesGPT Benchmark

Send a batch of questions concurrently to a direct HolmesGPT API and measure
first-text latency, completed-answer latency, and failures. Every run saves JSON;
`--export-to-excel` also creates a formatted workbook with metrics and answers.

## Install

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
holmes-benchmark --help
```

You can also run `python -m holmes_benchmark` after installation.
After updating an existing checkout, run `python -m pip install -e .` again to
install any new dependencies, including Rich for the live terminal display.

## Quick start

```bash
export HOLMES_API_URL="https://your-holmes-server/api/chat"
export HOLMES_API_TOKEN="your-token"  # Omit if your server doesn't require auth.

holmes-benchmark --parallel 5 \
  --question "Why is my pod restarting?" \
  --show-summary \
  --export-to-excel
```

This sends the same question five times concurrently. Each request starts a new
conversation; none reuses the answer or history from another request.

To mix questions, repeat `--question` (or `-q`):

```bash
holmes-benchmark -p 5 \
  -q "Why is my pod restarting?" \
  -q "Are any nodes under memory pressure?" \
  --export-to-excel
```

Assignment is **A, B, A, B, A**. At most one question per request can be supplied;
more questions than `--parallel` is an error. There is no built-in question:
provide at least one. Each invocation runs exactly one batch, with no warm-up,
automatic retries, or extra health-check requests.

While running, a live panel shows completed requests, elapsed time, and each
request's current activity. Tool names appear as HolmesGPT starts and finishes
them. "Last event" shows how long the server has been quiet. The completion bar
counts finished requests; it doesn't guess how far through an investigation the
model is. Up to ten requests fit in the panel, with active requests shown first.

When output is redirected or the terminal doesn't support the live display,
the CLI writes plain activity logs and a waiting update every five seconds to
stderr. Use `--no-progress` to disable both progress displays. This also avoids
progress-rendering overhead when comparing timings.

Each run opens with a separated **HolmesGPT Benchmarking** banner. A brief success
summary and output paths always appear at the end. Add `--show-summary` for a
readable per-request table with first-text, answer and elapsed times, tool/error
counts, and last reported input/output/total tokens, followed by numbered questions.
This works independently of Excel export, including with `--no-progress`:

```bash
holmes-benchmark -p 3 -q "Check pods" --show-summary
```

Missing token counts stay `N/A`; displayed tokens are the server's last usage
snapshot, not investigation totals. Failed requests stay visible even without
`--show-summary`.

## Try it locally with a mock server

From the project directory, start the standalone mock in **terminal 1**:

```bash
python3 scripts/mock_holmes.py
```

In **terminal 2**, use the actual CLI:

```bash
source .venv/bin/activate
unset HOLMES_API_TOKEN HOLMES_MODEL HOLMES_TIMEOUT
export HOLMES_API_URL="http://127.0.0.1:8081/api/chat"

holmes-benchmark -p 5 -q "Check pods" -q "Check nodes" --show-summary --export-to-excel
```

The mock returns fixed answers and simulated token counts, and shows a simulated
`kubernetes.list_pods` tool call. First text arrives after about 0.2 seconds and
the final answer after about 1 second. All five requests run
concurrently. Open the printed `report.xlsx` path to inspect the workbook.
Try changing `-p` and the questions, or add `--timeout 0.5` to see timeout results.
No real model, cluster, or API token is used. Stop the mock with Ctrl-C in terminal 1.
If port 8081 is occupied, use `--port 8082` and update `HOLMES_API_URL` accordingly.

## Configuration

| CLI option | Environment variable | Default |
|---|---|---|
| `--api-url` | `HOLMES_API_URL` | Required full endpoint, including `/api/chat` |
| `--question`, `-q` | — | Required; repeatable |
| `--parallel`, `-p` | — | `1` request |
| `--model` | `HOLMES_MODEL` | Server default |
| `--timeout` | `HOLMES_TIMEOUT` | `300` seconds per request |
| `--export-to-excel` | — | Off |
| `--no-progress` | — | Off; live progress is enabled |
| `--show-summary` | — | Off; show the detailed terminal results table |
| — | `HOLMES_API_TOKEN` | No authentication |

CLI options override their environment variables. Environment variables must be
exported in your shell; `.env` files are not automatically loaded. The API token
is sent as `Authorization: Bearer …`. URL userinfo and fragments are rejected;
authentication belongs in the token variable. HTTPS verification remains enabled
and redirects are not followed. Standard HTTP proxy environment settings apply.

For example, to select a server-configured model and allow ten minutes:

```bash
holmes-benchmark -q "Explain the failing deployments" -p 3 \
  --model fast-model --timeout 600 --export-to-excel
```

`--model` is the model name configured on your HolmesGPT server, which may be a
`modelList` alias rather than a provider's model identifier. The tool sends
`{"ask": "…", "stream": true}` plus `model` when specified, following the
[HolmesGPT HTTP API](https://github.com/HolmesGPT/holmesgpt/blob/master/docs/reference/http-api.md).

## What the numbers mean

- **First text:** elapsed time from sending the request to the first nonempty
  `ai_message.content`, or the final `analysis` if no earlier text arrives. This
  is **not first-token latency**: HolmesGPT may buffer text before emitting an event.
  Reasoning-only messages and heartbeat comments do not count as visible text.
- **Completed answer:** elapsed time through the fully received `ai_answer_end`
  event. The terminal labels this `Answer(s)`.
- **Elapsed:** time until that request terminates, including failed and cancelled
  requests. Failed requests have no completed-answer measurement.
- **Tool calls/errors:** observed tool calls, correlated by call ID. A tool failure
  doesn't itself fail the whole request if HolmesGPT subsequently completes its
  answer. No tool approvals or frontend tools are executed by this client.
- **Last reported tokens:** the latest `metadata.usage` snapshot when available.
  These values are not assumed to be cumulative across an investigation. No
  speculative cost or tokens-per-second calculation is made. Other metadata,
  tool results, and follow-up suggestions remain available in the JSON events.

Durations use a monotonic clock and include network and server latency. Batch
timing covers the concurrent requests, excluding client setup and report export.
The client allocates enough connections for the chosen parallelism. A request's
timeout is a total deadline, including a stream that continues sending heartbeats.

The summary reports success rate, total batch duration, successful requests per
second, and minimum/mean/median/p95/maximum completed-answer times. Latency
statistics include **successful requests only**, with their sample count shown.
P95 uses nearest rank (`ceil(0.95 × count)`); for small batches it is often the
maximum. Missing values display as `N/A` and are `null` in JSON.

These are performance measurements, not answer-quality scores. Workload mix,
server/model caches, rate limits, and available tools can affect comparisons.

## Results

Each run creates a unique directory relative to your current working directory:

```text
results/20261018T120000000000Z-a1b2c3d4/
├── results.json
└── report.xlsx       # With --export-to-excel
```

The schema-versioned JSON includes safe settings, UTC timestamps, the summary,
normalized request records, and raw SSE data events with receive times. Unknown
events are retained. Heartbeat comments are not data events. Final answers and
HTTP error bodies are preserved; authentication headers are never saved and the
configured token is redacted from echoed response data and error messages.
The results directory is excluded from Git by default.

The Excel workbook contains:

- **Summary:** clearly separated performance, run settings, and measurement notes.
- **Results:** filterable rows with outcomes, timings, tool counts,
  last reported tokens, and errors.
- **Answers:** questions, final answers, and errors. Long text continues in
  short, numbered reading sections with wrapped text and rows sized to fit;
  full details are also available in JSON. Partial assistant
  text from interrupted requests remains in the JSON events.

Sheets use Aptos fonts, navy/teal headers, alternating row shading, highlighted
answer times, and colored outcome cells. Gridlines are hidden and headers frozen.
Long questions and errors have concise previews in Results and full text in Answers.
The workbook keeps ordinary worksheet filters, without charts, merged cells,
or structured Excel table objects. Response text is written as
literal strings rather than spreadsheet formulas. Excel-incompatible characters
are removed from the workbook only. JSON retains them. Excel generation happens
after JSON saving; an export failure leaves the JSON available.

Press **Ctrl-C once** during a run to cancel outstanding requests and save
completed and partial results, including Excel if requested. A second interrupt
can force immediate exit before saving finishes.

| Exit code | Meaning |
|---|---|
| `0` | All requests succeeded and requested outputs were saved |
| `1` | A request, output preflight, or export failed |
| `2` | Invalid command-line or environment configuration |
| `130` | Interrupted |

HTTP errors, streamed errors (even with HTTP 200), malformed responses, timeouts,
network errors, and streams ending without a final answer are unsuccessful.
`approval_required` is an incomplete result, not an approval request made by this
tool. Other requests continue when one fails.

## Development and verification

```bash
python -m pip install -e '.[dev]'
python -m pytest -q
python -m pip wheel --no-deps --no-build-isolation . -w dist
```

Tests use local HTTP streaming servers rather than a real HolmesGPT deployment.
They cover concurrent dispatch, deterministic rotation, fragmented UTF-8/SSE,
timing, total deadlines, failure isolation, SIGINT partial output, credential
redaction, live tool activity, plain progress logs, CLI exit codes, and reopening
generated Excel workbooks with checks on their underlying XML structure.

To check your deployment, first run one representative question with `-p 1`,
inspect its answer and events, then increase parallelism with the same workload.
Supported contracts are the direct `/api/chat` SSE events; a Robusta platform URL
or custom gateway may require a different API integration.
