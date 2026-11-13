# RSSHub social-feed bridge (optional)

This bridge runs **inside the existing OpenShift namespace**. It is internal-only (ClusterIP), so it does not publish social sessions or an RSSHub endpoint to the Internet. The bot already supports RSS, so bridge output is consumed by `RssCollector` without adding another social scraper to the bot process.

## Capacity decision from the supplied OpenShift quota output

The deployment quota is `30Gi` memory request / `30` CPU limits, with the bot currently using `128Mi` requested memory and `50m` CPU. This manifest requests `768Mi` + `250m` for RSSHub and `64Mi` + `25m` for its Redis cache; limits are `1536Mi` + `1 CPU` and `256Mi` + `250m`. That is comfortably inside the reported quota. Storage quota is not increased: Redis is an ephemeral cache. **No separate host is indicated by these numbers.** Actual pod scheduling must still be confirmed in the cluster after applying the manifest.

## Pinned image and the Instagram hashtag shim

The image is pinned to `diygod/rsshub:2026-11-05` on purpose:

- RSSHub `2026-11-06` bumped `x-client-transaction-id` to `0.3.3`. Its X code still sets the old `isInitialized` flag, so every `/twitter/keyword/...` route fails with `ClientTransactionNotInitializedError` before contacting X. `2026-11-05` is the last image where both X keyword and X user routes return `200`.
- Instagram removed `/api/v1/tags/web_info/` (it now returns `404`). The replacement used here is `POST /api/v1/tags/<tag>/sections/`, which still returns live hashtag media.

RSSHub's `dist/index.mjs` overwrites `globalThis.fetch` while booting, so a normal `--import` preload cannot shim that request. `deploy/rsshub-boot.mjs` therefore loads RSSHub first and only then installs `deploy/instagram-tags-patch.mjs`. `deploy.ps1` / `deploy.sh` publish both files as the `rsshub-ig-patch` ConfigMap; the deployment mounts it at `/patch` and starts with `node --max-http-header-size=32768 /patch/rsshub-boot.mjs`.

Do not move the image to `:latest` until RSSHub fixes the transaction-id incompatibility. Re-test the X keyword and Instagram tag routes from the bot pod after any image change.

## 1. Deploy on the existing host

From the repository root, after `oc whoami` succeeds:

```powershell
# deploy.ps1 / deploy.sh also create the rsshub-ig-patch ConfigMap
oc apply -f deploy/social-feed-hub.yaml
oc rollout status deployment/social-feed-redis --timeout=180s
oc rollout status deployment/social-feed-hub --timeout=300s
oc get pods -l app=social-feed-hub
oc get pods -l app=social-feed-redis
```

No Route, LoadBalancer, or public ingress should be created. The bot reaches RSSHub at `http://social-feed-hub:1200` over the namespace network.

## 2. Add social login material without putting it in Git or chat

Telegram is read through RSSHub's public preview route (`/telegram/channel/<name>`), which needs **no** session or credential. Three live channels are enabled in `config/sources.yaml` (`social_tg_osintdefender`, `social_tg_theaviationist`, `social_tg_aviationiran`).

RSSHub's X and Instagram routes need platform session credentials and are the only reason `social-feed-secrets` exists:

- **X:** `TWITTER_AUTH_TOKEN` — the `auth_token` cookie from a browser logged in at `x.com` (DevTools → Application → Cookies → `https://x.com`).
- **Instagram:** `INSTAGRAM_COOKIE` — only `sessionid` and `ds_user_id` are required, formatted as `sessionid=...; ds_user_id=...` (DevTools → Application → Cookies → `https://www.instagram.com`).

Put those two keys in the untracked `.env` on your own machine. `deploy.ps1` and `deploy.sh` read only those keys, write them into the `social-feed-secrets` secret with `oc set data` (leaving any other keys untouched), and restart RSSHub so the new values are loaded. Never paste credentials into a conversation or commit them.

Manual refresh, if you are not running a deploy:

```powershell
oc set data secret/social-feed-secrets --from-literal=TWITTER_AUTH_TOKEN='...' --from-literal=INSTAGRAM_COOKIE='...'
oc rollout restart deployment/social-feed-hub
oc rollout status deployment/social-feed-hub --timeout=300s
```

Sessions expire or get challenged by the platforms. A `401` from `x.com` or an Instagram redirect loop/`429` means the credential is stale; refresh it once and redeploy. Do not repeatedly probe a rate-limited Instagram route from the cluster IP.

## 3. Test before enabling any feeds

RSSHub routes are upstream-dependent; test every route from the bot pod first. Example for Anadolu's X profile:

```powershell
$pod = oc get pod -l app=airwatch -o jsonpath='{.items[0].metadata.name}'
oc exec $pod -- python -c "import urllib.request; u='http://social-feed-hub:1200/twitter/user/anadoluagency'; r=urllib.request.urlopen(u,timeout=20); print(r.status, r.headers.get('Content-Type')); print(r.read(500).decode('utf-8','replace'))"
```

Test each route from the bot pod. The Anadolu X and Instagram profile feeds are the live-tested profile pilot; other candidate profile feeds remain disabled until individually validated. RSSHub's profile routes are `/twitter/user/:handle` and `/instagram/user/:handle`; upstream changes may require updating these paths.

## Active hashtag feeds and rate controls

The project monitors 25 configured hashtag terms across X and Instagram (50 RSSHub routes), covering major Iranian airports plus general and Persian variants. The enabled X and Instagram profile pilot feeds are included in the same social job; they are not polled by the 75-second general-RSS job. A dedicated airport hashtag feed bypasses keyword thresholds by design, so source text must still pass the general relevance classifier.

X and Instagram RSSHub hashtag/profile routes are polled once per hour with a single worker. The three Telegram public-preview RSS sources are ordinary RSS sources and poll every 75 seconds. Telegram publication of X/Instagram posts is independently limited to one post per network per hour (up to 24 posts/day per network), including the profile pilots; additional eligible events wait for later slots. Each route may return up to 30 entries, limited to the latest 12 hours. RSSHub cache remains 120 seconds. General RSS polling remains at 75 seconds.

RSSHub is configured with `REQUEST_RETRY=0` so an upstream Instagram 429 is not immediately retried internally. `REQUEST_RATE_LIMITS` spaces requests to `www.instagram.com` at one start every two seconds. The RSSHub hashtag `tags` route normally makes two upstream calls per route and may make one additional cold-session call; 25 tag routes therefore mean about 50–53 upstream starts per cycle (plus any enabled profile route), spread over roughly 100–106 seconds. These are route-source estimates and can change with RSSHub updates.

On the first failed Instagram or X route, the bot skips the remaining routes for that same platform in the current cycle and puts all routes for that platform into a 15-minute cooldown. With the one-hour poll schedule, the next attempt normally waits until the next cycle. Instagram 429 is an upstream rejection, not something a cookie or IP rotation can reliably fix; do not repeatedly probe the route or rotate sessions/IPs. These controls reduce repeated traffic but cannot guarantee Instagram will accept the session or IP. `deploy.ps1` and `deploy.sh` apply the RSSHub/Redis manifest and wait for rollout.

To re-test routes from the bot pod:

```powershell
$pod = oc get pod -l app=airwatch -o jsonpath='{.items[0].metadata.name}'
oc exec $pod -- python -c "import requests,feedparser; u='http://social-feed-hub:1200/twitter/keyword/%23IranAirspace'; r=requests.get(u,timeout=30); f=feedparser.parse(r.content); print('HTTP',r.status_code,'items',len(f.entries)); print(f.feed.get('title'))"
oc exec $pod -- python -c "import requests,feedparser; u='http://social-feed-hub:1200/instagram/tags/IranAirspace'; r=requests.get(u,timeout=30); f=feedparser.parse(r.content); print('HTTP',r.status_code,'items',len(f.entries)); print(f.feed.get('title'))"
```

The `watchlist` also feeds optional native X/Instagram collectors, which remain disabled unless their own `ENABLE_X` / `ENABLE_INSTAGRAM` flags are explicitly enabled. The RSSHub hashtag feeds are the active social path.

## 4. Monitor and roll back

```powershell
oc logs -f deployment/social-feed-hub
oc logs -f deployment/social-feed-redis
oc get pods
```

The sandbox Idler can set `spec.replicas=0` on any Deployment. `CronJob/airwatch-keepalive` now checks all three deployments every five minutes (`KEEPALIVE_DEPLOYMENTS=airwatch,social-feed-hub,social-feed-redis` in `deploy/app.yaml`), so the bridge does not silently disappear while the news bot stays alive.

Rollback the optional bridge without touching the news bot:

```powershell
oc delete -f deploy/social-feed-hub.yaml
```

Then disable/remove the `social_*` RSS rows. The original RSS and GDELT collectors continue independently.
