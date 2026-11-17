// Cloudflare Worker: GitHub Actions زمانبندی داخلی گاهی دقائق یا ساعتها تأخیر
// دارد/اجرا را حذف میکند. این Worker هر ۵ دقیقه یک repository_dispatch میزند
// تا پالس دقیقاً سر وقت اجرا شود. هزینهای ندارد (Workers Free + Cron Trigger).

export const DISPATCH_URL =
  "https://api.github.com/repos/10to1000k/iran-airspace-watch/dispatches";

export default {
  async scheduled(_controller, env, _ctx) {
    const response = await fetch(DISPATCH_URL, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_PAT}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "airwatch-pulse-cron",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ event_type: "airwatch-tick" }),
    });

    if (!response.ok) {
      console.error(
        `repository_dispatch failed: ${response.status} ${await response.text()}`,
      );
    }
  },
};
