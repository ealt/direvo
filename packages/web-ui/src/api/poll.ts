interface PollState {
  etag: string;
  url: string;
}

export function createPoller(url: string): PollState {
  return { etag: "", url };
}

export async function hasChanged(state: PollState): Promise<boolean> {
  try {
    const response = await fetch(state.url, {
      method: "HEAD",
      headers: state.etag ? { "If-None-Match": state.etag } : {},
    });
    const newEtag = response.headers.get("etag") ?? "";
    if (response.status === 304) {
      return false;
    }
    if (newEtag && newEtag !== state.etag) {
      state.etag = newEtag;
      return true;
    }
    // No ETag support — check last-modified
    return response.ok;
  } catch {
    return false;
  }
}
