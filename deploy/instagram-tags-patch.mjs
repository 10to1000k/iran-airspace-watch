// Runtime shim for RSSHub's Instagram hashtag route.
//
// Instagram removed /api/v1/tags/web_info/ (it now returns 404). The replacement
// that still returns live hashtag media is:
//
//   POST https://www.instagram.com/api/v1/tags/<tag>/sections/
//
// Response shapes differ:
//   route expects: { data: { profile_pic_url, top: { sections: [...] } } }
//   endpoint gives: { sections: [...], more_available, ... }
//
// This module rewrites only that one upstream request and leaves every other
// fetch untouched. It must be installed AFTER RSSHub's own startup, because
// dist/index.mjs replaces globalThis.fetch while booting. See rsshub-boot.mjs.

export function installInstagramTagsPatch() {
    if (globalThis.__airwatchIgTagsPatchInstalled) return;
    globalThis.__airwatchIgTagsPatchInstalled = true;

    const originalFetch = globalThis.fetch;

    const headersFrom = (input, init) => {
        if (init?.headers) return new Headers(init.headers);
        if (input && typeof input === 'object' && input.headers) return new Headers(input.headers);
        return new Headers();
    };

    const csrfFromHome = async (cookie) => {
        const home = await originalFetch('https://www.instagram.com/', {
            headers: {
                cookie,
                'user-agent':
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
                accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            },
            redirect: 'follow',
        });
        const setCookies = home.headers.getSetCookie ? home.headers.getSetCookie() : [];
        for (const item of setCookies) {
            const match = /(?:^|;\s*)csrftoken=([^;]+)/.exec(item);
            if (match) return match[1];
        }
        const html = await home.text();
        return /"csrf_token":"([A-Za-z0-9]+)"/.exec(html)?.[1] || '';
    };

    globalThis.fetch = async function airwatchPatchedFetch(input, init = {}) {
        let url;
        try {
            url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
        } catch {
            return originalFetch(input, init);
        }

        if (!url.startsWith('https://www.instagram.com/api/v1/tags/web_info/')) {
            return originalFetch(input, init);
        }

        try {
            const parsed = new URL(url);
            const tag = parsed.searchParams.get('tag_name') || '';
            if (!tag) return originalFetch(input, init);

            const headers = headersFrom(input, init);
            const cookie = headers.get('cookie') || '';
            let csrf = /(?:^|;\s*)csrftoken=([^;]+)/.exec(cookie)?.[1] || '';
            if (!csrf) csrf = await csrfFromHome(cookie);

            const sectionsHeaders = new Headers(headers);
            sectionsHeaders.set('content-type', 'application/x-www-form-urlencoded');
            if (csrf) sectionsHeaders.set('x-csrftoken', csrf);

            const upstream = await originalFetch(
                `https://www.instagram.com/api/v1/tags/${encodeURIComponent(tag)}/sections/`,
                { ...init, method: 'POST', headers: sectionsHeaders, body: '', redirect: 'follow' }
            );
            const text = await upstream.text();
            if (!upstream.ok) {
                console.error(`[ig-tags-patch] sections ${tag}: HTTP ${upstream.status}`);
                return new Response(text, {
                    status: upstream.status,
                    headers: {
                        'content-type': upstream.headers.get('content-type') || 'text/plain',
                    },
                });
            }

            const payload = JSON.parse(text);
            console.log(
                `[ig-tags-patch] ${tag}: ${(payload.sections || []).length} sections`
            );
            return new Response(
                JSON.stringify({
                    data: {
                        profile_pic_url: null,
                        top: { sections: payload.sections || [] },
                    },
                }),
                { status: 200, headers: { 'content-type': 'application/json' } }
            );
        } catch (error) {
            console.error(`[ig-tags-patch] failed: ${error?.message || error}`);
            return originalFetch(input, init);
        }
    };
}
