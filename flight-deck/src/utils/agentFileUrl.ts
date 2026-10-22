/** A file on an agent's disk, viewable in the browser through Flight Deck's
 *  proxy (chat images, the queue's picture thumbnails). */
export function agentFileViewUrl(path: string, host?: string, port?: number, auth?: string): string {
  const params = new URLSearchParams({ path })
  if (auth) params.set('token', auth)
  return `/fd/agent-file-view/${encodeURIComponent(host || 'localhost')}/${port}?${params}`
}
