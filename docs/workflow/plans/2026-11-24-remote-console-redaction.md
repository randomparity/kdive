# Remote console redaction scheduling plan

Expected implementation size: 90–150 changed lines (S).
One independently committed queue task; no prerequisite implementation from other tasks.

## Verification

Mode: focused-test. Offload/progress/order/freshness/redaction/errors/cancellation are tested
in tests/providers/remote_libvirt/console/test_console_read.py. The synchronous implementation
must fail the worker-thread assertion before any blocking coordination begins.
Green: just test-verbose tests/providers/remote_libvirt/console/test_console_read.py.
Expect all cases to pass without skipped tests. Then just lint and just type must exit zero.

## Interfaces

Consume RemoteLibvirtConsoleReader.read_window(conn, system_id, start_index=0), _redact(bytes),
SecretRegistry.register(value, scope=None) and ConsoleWindowRead(data, next_index, pumped).
Retain all signatures. No later task depends on new names, only offloaded scheduling.

## Implementation

Add threading import to the existing test file. Add a parametrized async scenario invoked
with asyncio.run: block a wrapped real _redact using threading.Event, signal entry using
loop.call_soon_threadsafe(asyncio.Event.set), assert worker identity before blocking, await
entry while the task remains incomplete, register the probe secret before entry, release
the worker in a finally block and await the result. Assert masked bytes, cursor and pumped.
Bound waits with asyncio.wait_for and Event.wait; never use sleeps. Reuse the same wrapper
for cancellation: cancel after worker entry, expect CancelledError, release and drain the
executor through asyncio.run cleanup. Patch the reader instance only, not asyncio or registry.
Add a parametrized failure test that raises the exact sentinel exception in probe or redactor,
asserts identity at the read boundary, and asserts no redactor call after probe failure.
Keep malformed UTF-8, empty-window, store-failure and cursor tests unchanged.

In src/kdive/providers/remote_libvirt/console/read.py replace the synchronous return with:

```python
        data = await asyncio.to_thread(self._redact, data)
        return ConsoleWindowRead(data, next_index, pumped)
```

Update read_window's docstring to state that redaction also runs in a worker thread after
the probe. No new lock or cached Redactor is required. If a check fails, diagnose before
changing the implementation. Run the original focused file and whole-tree lint/type checks.
For a controlled fault, temporarily restore the synchronous return, run the thread/progress
test expecting failure, restore exact fixed bytes in finally, and rerun that test successfully.
Review the complete task diff independently; fix defensible findings before committing.

## Completion and rollback

Stage only these design records, then source and its test in a separate conventional commit.
Let installed hooks run. Resolve the exact finding and record its source commit. Do not mark
the provider-rewiring or assessment steps complete. Rollback is a normal revert; no data cleanup.
