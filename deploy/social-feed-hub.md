# RSSHub social-feed bridge (optional)

This bridge runs **inside the existing OpenShift namespace**. It is internal-only (ClusterIP), so it does not publish social sessions or an RSSHub endpoint to the Internet. The bot already supports RSS, so bridge output is consumed by `RssCollector` without adding another social scraper to the bot process.

## Capacity decision from the supplied OpenShift quota output

The deployment quota is `30Gi` memory request / `30` CPU limits, with the bot currently using `128Mi` requested memory and `50m` CPU. This manifest requests `768Mi` + `250m` for RSSHub and `64Mi` + `25m` for its Redis cache; limits are `1536Mi` + `1 CPU` and `256Mi` + `250m`. That is comfortably inside the reported quota. Storage quota is not increased: Redis is an ephemeral cache. **No separate host is indicated by these numbers.** Actual pod scheduling must still be confirmed in the cluster after applying the manifest.

## 1. Deploy on the existing host

From the repository root, after `oc whoami` succeeds:

```powershell
oc apply -f deploy/social-feed-hub.yaml
oc rollout status deployment/social-feed-redis --timeout=180s
oc rollout status deployment/social-feed-hub --timeout=300s
oc get pods -l app=social-feed-hub
oc get pods -l app=social-feed-redis
```

No Route, LoadBalancer, or public ingress should be created. The bot reaches RSSHub at `http://social-feed-hub:1200` over the namespace network.

## 2. Add social login material without putting it in Git or chat

RSSHub's X/Instagram routes can require platform session credentials. RSSHub documents X login variables (`TWITTER_USERNAME`, `TWITTER_PASSWORD`, optionally `TWITTER_PHONE_OR_EMAIL` and `TWITTER_AUTHENTICATION_SECRET`) and Instagram cookies (`INSTAGRAM_COOKIE`, including `sessionid` and `ds_user_id`). These are sensitive account credentials; use a low-value/dedicated account, understand that either platform can revoke sessions, and never paste credentials into a conversation or commit them.

Create a local, untracked file `.social-feed-secrets.env` with only the variables you actually need, then apply it:

```powershell
oc create secret generic social-feed-secrets `
  --from-env-file=.social-feed-secrets.env `
  --dry-run=client -o yaml | oc apply -f -
Remove-Item .social-feed-secrets.env -Force
oc rollout restart deployment/social-feed-hub
oc rollout status deployment/social-feed-hub --timeout=300s
```

Add credentials only on your own machine. Do not place the file in a shared folder. Session-based collection is unofficial and can break or trigger account challenges; it is not equivalent to official API access.

## 3. Test before enabling any feeds

RSSHub routes are upstream-dependent; test every route from the bot pod first. Example for Anadolu's X profile:

```powershell
$pod = oc get pod -l app=airwatch -o jsonpath='{.items[0].metadata.name}'
oc exec $pod -- python -c "import urllib.request; u='http://social-feed-hub:1200/twitter/user/anadoluagency'; r=urllib.request.urlopen(u,timeout=20); print(r.status, r.headers.get('Content-Type')); print(r.read(500).decode('utf-8','replace'))"
```

Test each route from the bot pod. The Anadolu X and Instagram profile feeds are the live-tested profile pilot; other candidate profile feeds remain disabled until individually validated. RSSHub's profile routes are `/twitter/user/:handle` and `/instagram/user/:handle`; upstream changes may require updating these paths.

## Active hashtag feeds and rapid polling

The project monitors 25 configured hashtag terms across X and Instagram (50 RSSHub routes), covering major Iranian airports plus general and Persian variants. The running deployment has returned HTTP 200 for many routes on both platforms; some X searches such as `#TehranFIR` and two Persian flight tags return RSSHub 503 `route is empty`. These per-tag failures do not stop successful feeds. Every returned item from a dedicated airport hashtag feed is publishable and bypasses keyword thresholds and topic-title vetoes, so hashtag misuse can create noise.

To publish more quickly, hashtag feeds run in their own 30-second collector cycle (general RSS stays at 75 seconds). Each hashtag feed can contribute up to 30 entries, limited to the latest 12 hours. RSSHub route cache is set to 120 seconds; effective end-to-end delay can therefore still be roughly 2.5 minutes plus upstream and Telegram response time. This is an aggressive polling rate and may trigger X/Instagram throttling. There are 25 unique Instagram hashtag searches, within the documented 30-search rolling-week limit; repeated requests may still be rate-limited. If Instagram returns 429 or login pages, disable affected rows and let the cooldown pass. `deploy.ps1` and `deploy.sh` now apply the RSSHub/Redis manifest and wait for rollout as part of deployment.

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

Rollback the optional bridge without touching the news bot:

```powershell
oc delete -f deploy/social-feed-hub.yaml
```

Then disable/remove the `social_*` RSS rows. The original RSS and GDELT collectors continue independently.
