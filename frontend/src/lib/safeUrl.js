// Text from the web can contain any URL. Only http(s) ever becomes a clickable
// link; javascript:, data:, file: and anything unparseable stay plain text,
// so clicking something the tracker found can never run code in this app.
export function safeHref(url) {
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : null
  } catch {
    return null
  }
}

export function hostOf(url) {
  try {
    return new URL(url).hostname
  } catch {
    return url
  }
}
