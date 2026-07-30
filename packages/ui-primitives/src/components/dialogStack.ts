const openDialogs: symbol[] = []

export function registerDialog(token: symbol): void {
  unregisterDialog(token)
  openDialogs.push(token)
}

export function unregisterDialog(token: symbol): boolean {
  const index = openDialogs.lastIndexOf(token)
  if (index < 0) return false
  const topmost = index === openDialogs.length - 1
  openDialogs.splice(index, 1)
  return topmost
}

export function isTopmostDialog(token: symbol): boolean {
  return openDialogs.at(-1) === token
}
