// Browser-engine bootstrap shared by native SDKs. Runs on https://duck.ai only.
// `journey` is a per-client random identifier passed by the native host.
if (location.origin !== 'https://duck.ai') throw new Error('Unexpected bootstrap origin');
const started = Date.now();
const frame = document.querySelector('iframe#jsa');
if (!frame || !frame.contentWindow) throw new Error('Duck.ai challenge iframe is unavailable');
const headers = {'x-ddg-journey-id': journey, accept: 'application/json'};
const auth = await fetch('/duckchat/v1/auth/token', {headers, cache: 'no-store', redirect: 'error'});
await auth.arrayBuffer();
if (!auth.ok) return {status: auth.status, error: 'Duck.ai rejected anonymous bootstrap', retryAfter: auth.headers.get('retry-after')};
const status = await fetch('/duckchat/v1/status', {headers: {...headers, 'x-vqd-accept': '1'}, cache: 'no-store', redirect: 'error'});
await status.arrayBuffer();
if (!status.ok) return {status: status.status, error: 'Duck.ai rejected anonymous bootstrap', retryAfter: status.headers.get('retry-after')};
const challenge = status.headers.get('x-vqd-hash-1');
if (!challenge) throw new Error('Duck.ai returned no browser challenge');
// The site iframe permits inline scripts and forbids eval. Run the expression
// there, then normalize the result in the parent page, as the frontend does.
const key = '__duckai_' + Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
const script = frame.contentDocument.createElement('script');
const challengeStarted = Date.now();
const result = await new Promise((resolve, reject) => {
  window[key] = (error, value) => error ? reject(error) : resolve(value);
  script.textContent = 'try { window.parent[' + JSON.stringify(key) + '](null, ' + atob(challenge) + '); } catch (error) { window.parent[' + JSON.stringify(key) + '](error); }';
  frame.contentDocument.body.appendChild(script);
}).finally(() => {script.remove(); delete window[key];});
if (!result || !Array.isArray(result.client_hashes)) throw new Error('Unexpected Duck.ai challenge result');
await new Promise(resolve => setTimeout(resolve, 0));
const hashes = await Promise.all(result.client_hashes.map(async value => {
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value)));
  return btoa(Array.from(digest, byte => String.fromCharCode(byte)).join(''));
}));
const error = new Error();
const lines = (error.stack || 'no-stack').split('\n');
const skip = lines[0].includes(error.message || 'Unknown') ? 1 : 0;
const stack = lines.slice(skip, skip + 5).map(line => line.trim()).join('\n') +
  (lines.length > skip + 5 ? `\n... (${lines.length - skip - 5} more frames omitted)` : '');
const token = btoa(JSON.stringify({...result, client_hashes: hashes, meta: {...result.meta,
  origin: window.top.location.origin, stack, duration: String(Date.now() - challengeStarted)}}));
return {headers: {
  'x-vqd-hash-1': token,
  'x-fe-version': `${document.documentElement.getAttribute('data-version-tag')}-${document.documentElement.getAttribute('data-version-sha')}`,
  'x-fe-signals': btoa(JSON.stringify({start: started, events: [], end: Date.now() - started})),
  'x-ddg-journey-id': journey,
  'user-agent': navigator.userAgent
}};
