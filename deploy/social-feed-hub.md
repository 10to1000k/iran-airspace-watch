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

Try one at a time from `config/sources.yaml`. All proposed social RSS rows are currently `enabled: false` on purpose. Enable only routes that return a valid RSS/Atom document and relevant recent posts; then build and roll out the bot. If the response is an error, empty, HTML login page, or rate-limit response, leave that row disabled.

The live-tested Anadolu X and Instagram routes are enabled as a pilot in `config/sources.yaml`. Other candidate feeds for Mehr, IRNA, TASS, Al Jazeera English, and Iran International (X), and Mehr, IRNA, TASS, and Al Jazeera English (Instagram), remain disabled until individually tested. ISNA is not enabled because the English account identity still needs first-party confirmation. RSSHub's currently documented routes are `/twitter/user/:handle` and `/instagram/user/:handle`; upstream route changes may require updating these paths.

## Hashtag-feed pilot

The project defines monitored terms for Tehran airports, Iran flights and Persian/English variants. Candidate RSSHub hashtag routes are listed in `config/sources.yaml` but intentionally remain `enabled: false` until their exact paths return valid feeds in the live cluster. They carry a `watched_hashtags` marker so a matched platform tag is preserved for the relevance filter and included in the Telegram post.

The pilot uses at most 11 distinct Instagram tags, below Meta's documented 30 unique hashtag searches per rolling 7 days. Test one known tag per platform from the bot pod before enabling the rows. For example:

```powershell
$pod = oc get pod -l app=airwatch -o jsonpath='{.items[0].metadata.name}'
oc exec $pod -- python -c "import requests,feedparser; u='http://social-feed-hub:1200/twitter/keyword/%23IranAirspace'; r=requests.get(u,timeout=30); f=feedparser.parse(r.content); print('HTTP',r.status_code,'items',len(f.entries)); print(f.feed.get('title'))"
oc exec $pod -- python -c "import requests,feedparser; u='http://social-feed-hub:1200/instagram/tags/IranAirspace'; r=requests.get(u,timeout=30); f=feedparser.parse(r.content); print('HTTP',r.status_code,'items',len(f.entries)); print(f.feed.get('title'))"
```

Only enable hashtag source rows that return HTTP 200 and parse as RSS. If Instagram gives 429, stop retrying and let the cooldown pass; the rows stay off until validated. The `watchlist` also feeds the optional native X/Instagram collectors, but these remain disabled unless their own `ENABLE_X` / `ENABLE_INSTAGRAM` flags are explicitly enabled.

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
