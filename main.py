from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
import time
import asyncio
import threading
import os
from concurrent.futures import ThreadPoolExecutor

# OpenTelemetry imports
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

# Prometheus client for simple metrics endpoint
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

# Setup basic Prometheus metrics
REQUEST_COUNTER = Counter("demo_requests_total", "Total requests", ["endpoint"])
REQUEST_LATENCY = Histogram(
    "demo_request_latency_seconds", "Request latency", ["endpoint"]
)

# OpenTelemetry tracer setup
service_name = os.environ.get("OTEL_SERVICE_NAME", "fastapi-sync-async-demo")
resource = Resource.create({"service.name": service_name})
provider = TracerProvider(resource=resource)
trace.set_tracer_provider(provider)

# Export spans to console for local teaching. Also conditionally add OTLP exporter
provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
if otlp_endpoint:
    otlp_exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(otlp_exporter))

tracer = trace.get_tracer(__name__)

app = FastAPI()
executor = ThreadPoolExecutor(max_workers=4)

# Instrument FastAPI app (automatic HTTP spans)
FastAPIInstrumentor.instrument_app(app)


# Helper to capture simple runtime info
def runtime_info(tag: str):
    return {
        "tag": tag,
        "pid": os.getpid(),
        "thread_name": threading.current_thread().name,
        "thread_ident": threading.get_ident(),
        "time": time.time(),
    }


# Simple middleware to log request start and end with timing and register Prometheus metrics
@app.middleware("http")
async def simple_logger(request: Request, call_next):
    start = time.time()
    path = request.url.path
    with tracer.start_as_current_span("http.request", attributes={"http.target": path}):
        response = await call_next(request)
    elapsed = time.time() - start
    REQUEST_COUNTER.labels(endpoint=path).inc()
    REQUEST_LATENCY.labels(endpoint=path).observe(elapsed)
    print(
        f"[{time.strftime('%H:%M:%S')}] {request.method} {path} handled in {elapsed:.3f}s on thread {threading.current_thread().name}"
    )
    return response


# 1) Synchronous endpoint that performs a blocking IO wait
@app.get("/sync-blocking")
def sync_blocking(delay: float = 2.0):
    with tracer.start_as_current_span("sync_blocking_handler") as span:
        span.set_attribute("handler.type", "sync")
        span.set_attribute("delay", float(delay))
        info_start = runtime_info("sync-blocking-start")
        # time.sleep blocks the worker thread completely
        time.sleep(delay)
        info_end = runtime_info("sync-blocking-end")
        span.add_event("sleep_complete", attributes={"elapsed": delay})
        return JSONResponse(
            {
                "start": info_start,
                "end": info_end,
                "note": "time.sleep blocked the worker thread",
            }
        )


# 2) Async endpoint that incorrectly uses blocking call
@app.get("/async-blocking-wrong")
async def async_blocking_wrong(delay: float = 2.0):
    with tracer.start_as_current_span("async_blocking_wrong_handler") as span:
        span.set_attribute("handler.type", "async-wrong")
        span.set_attribute("delay", float(delay))
        info_start = runtime_info("async-blocking-wrong-start")
        # This blocks the event loop even though the handler is async
        time.sleep(delay)
        info_end = runtime_info("async-blocking-wrong-end")
        span.add_event("blocking_sleep_complete", attributes={"elapsed": delay})
        return JSONResponse(
            {
                "start": info_start,
                "end": info_end,
                "note": "time.sleep blocked the event loop",
            }
        )


# 3) Proper async non-blocking sleep
@app.get("/async-nonblocking")
async def async_nonblocking(delay: float = 2.0):
    with tracer.start_as_current_span("async_nonblocking_handler") as span:
        span.set_attribute("handler.type", "async")
        span.set_attribute("delay", float(delay))
        info_start = runtime_info("async-nonblocking-start")
        # await asyncio.sleep does not block the event loop
        await asyncio.sleep(delay)
        info_end = runtime_info("async-nonblocking-end")
        span.add_event("async_sleep_complete", attributes={"elapsed": delay})
        return JSONResponse(
            {
                "start": info_start,
                "end": info_end,
                "note": "await asyncio.sleep allowed other tasks to run",
            }
        )


# 4) CPU bound synchronous work
@app.get("/cpu-bound-sync")
def cpu_bound_sync(iterations: int = 5_000_000):
    with tracer.start_as_current_span("cpu_bound_sync_handler") as span:
        span.set_attribute("handler.type", "cpu-sync")
        span.set_attribute("iterations", int(iterations))
        info_start = runtime_info("cpu-bound-sync-start")
        acc = 0
        for i in range(iterations):
            acc += i * i
        info_end = runtime_info("cpu-bound-sync-end")
        span.set_attribute("result_sample", int(acc % 1000))
        return JSONResponse(
            {
                "start": info_start,
                "end": info_end,
                "result_sample": acc % 1000,
                "note": "CPU bound computed on the worker thread",
            }
        )


# 5) CPU bound offloaded to threadpool from async handler
@app.get("/cpu-bound-offload")
async def cpu_bound_offload(iterations: int = 5_000_000):
    with tracer.start_as_current_span("cpu_bound_offload_handler") as span:
        span.set_attribute("handler.type", "cpu-offload")
        span.set_attribute("iterations", int(iterations))
        info_start = runtime_info("cpu-bound-offload-start")

        def work(n):
            acc = 0
            for i in range(n):
                acc += i * i
            return acc

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(executor, work, iterations)
        info_end = runtime_info("cpu-bound-offload-end")
        span.set_attribute("result_sample", int(result % 1000))
        return JSONResponse(
            {
                "start": info_start,
                "end": info_end,
                "result_sample": result % 1000,
                "note": "CPU bound work run in threadpool via run_in_executor",
            }
        )


# 6) Failing request to demonstrate tracing
@app.get("/fail")
def fail_request():
    with tracer.start_as_current_span("fail_request_handler") as span:
        span.set_attribute("handler.type", "fail")
        # Simulate an error
        raise ValueError("This is a simulated failure for tracing demo")


# Prometheus metrics endpoint
@app.get("/metrics")
def metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)


# Small HTML teaching UI to exercise endpoints concurrently
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>FastAPI Sync vs Async Demo (OpenTelemetry)</title>
  <style>
    body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; padding: 16px }
    button { margin: 4px }
    pre { background: #f6f8fa; padding: 8px; border-radius: 6px }
  </style>
</head>
<body>
  <h1>FastAPI Sync vs Async Demo (OpenTelemetry)</h1>
  <p>Use the buttons to fire single or concurrent requests to different endpoints and watch timestamps and thread names returned by the server. Traces are exported to console and/or OTLP if OTEL_EXPORTER_OTLP_ENDPOINT is set. Metrics available at <code>/metrics</code>.</p>

  <div>
    <label>Delay / Iterations: <input id="param" value="2" /></label>
  </div>

  <div>
    <button onclick="call('/sync-blocking')">Call sync-blocking once</button>
    <button onclick="callMultiple('/sync-blocking')">Call sync-blocking x5 concurrently</button>
  </div>

  <div>
    <button onclick="call('/async-blocking-wrong')">Call async-blocking-wrong once</button>
    <button onclick="callMultiple('/async-blocking-wrong')">Call async-blocking-wrong x5 concurrently</button>
  </div>

  <div>
    <button onclick="call('/async-nonblocking')">Call async-nonblocking once</button>
    <button onclick="callMultiple('/async-nonblocking', 20)">Call async-nonblocking x20 concurrently</button>
  </div>

  <div>
    <button onclick="call('/cpu-bound-sync?iterations=1000000')">Call cpu-bound-sync</button>
    <button onclick="call('/cpu-bound-offload?iterations=1000000')">Call cpu-bound-offload</button>
  </div>

  <h2>Results</h2>
  <pre id="out"></pre>

  <script>
const out = document.getElementById('out')
function log(s){ out.textContent = new Date().toISOString() + ' ' + s + '\\n' + out.textContent }

    async function call(path){
      const p = document.getElementById('param').value
      const url = path.includes('?') ? path + '&delay=' + p : path + '?delay=' + p
      log('fetch ' + url)
      const t0 = performance.now()
      try{
        const res = await fetch(url)
        const json = await res.json()
        const t1 = performance.now()
        log(`done ${path} in ${(t1-t0).toFixed(1)}ms\\n` + JSON.stringify(json, null, 2))
      } catch(e) {
        log('error ' + e)
      }
    }

    function callMultiple(path, n=5){
      const promises = []
      for(let i=0;i<n;i++) promises.push(call(path))
      return Promise.all(promises)
    }
  </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(INDEX_HTML)
