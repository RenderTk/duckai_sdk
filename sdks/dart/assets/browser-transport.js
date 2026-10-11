// Native hosts pass requestId, requestHeaders, requestBody, maxBytes and emit.
// emit is a host-specific function expression supplied by each SDK adapter.
if (location.origin !== 'https://duck.ai') throw new Error('Unexpected chat origin');
window.__duckaiRequests = window.__duckaiRequests || Object.create(null);
const controller = new AbortController();
window.__duckaiRequests[requestId] = controller;
let reader;
try {
  const response = await fetch('/duckchat/v1/chat', {
    method: 'POST', headers: requestHeaders, body: JSON.stringify(requestBody),
    signal: controller.signal, redirect: 'error', cache: 'no-store'
  });
  const headers = {};
  for (const [key, value] of response.headers) headers[key.toLowerCase()] = value;
  await emit({id: requestId, kind: 'headers', status: response.status, headers});
  if (!response.body) throw new Error('Duck.ai returned no response body');
  reader = response.body.getReader();
  let received = 0;
  while (true) {
    const {done, value} = await reader.read();
    if (done) break;
    received += value.byteLength;
    if (received > maxBytes) throw new Error('Duck.ai response exceeds collection limit');
    // Bound native bridge messages independently of the upstream chunk size.
    for (let offset = 0; offset < value.length; offset += 16384) {
      const chunk = value.subarray(offset, offset + 16384);
      await emit({id: requestId, kind: 'data', data: btoa(Array.from(chunk, byte => String.fromCharCode(byte)).join(''))});
    }
  }
  await emit({id: requestId, kind: 'done'});
} catch (error) {
  await emit({id: requestId, kind: 'error', error: controller.signal.aborted ? 'cancelled' : String(error)});
} finally {
  if (reader) { try { await reader.cancel(); } catch (_) {} reader.releaseLock(); }
  delete window.__duckaiRequests[requestId];
}
