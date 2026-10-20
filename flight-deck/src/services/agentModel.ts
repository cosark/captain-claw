// An agent's model block as Flight Deck reads it from its config.yaml —
// shared by the agent cards' ModelSelector and the kiosk's agent Settings.

export interface ModelInfo {
  provider: string
  model: string
  api_key: string
  base_url: string
  // Does the model see images itself (model.vision): 'auto' | 'on' | 'off'
  vision: string
}

export const VISION_OPTIONS = [
  { value: 'auto', label: 'Auto — known multimodal models' },
  { value: 'on', label: 'On — the model sees images itself' },
  { value: 'off', label: 'Off — a vision model describes them' },
]

export function parseModelFromYaml(yaml: string): ModelInfo {
  const info: ModelInfo = { provider: '', model: '', api_key: '', base_url: '', vision: 'auto' }
  // Simple YAML parser for the model section
  const lines = yaml.split('\n')
  let inModel = false
  for (const line of lines) {
    const trimmed = line.trimStart()
    // Entering/leaving the top-level model section — judged on the raw line:
    // its keys are indented, the next section's key is not.
    if (/^model\s*:/.test(line)) {
      inModel = true
      continue
    }
    if (inModel && trimmed.length > 0 && !/^[\s#]/.test(line)) {
      inModel = false
    }
    if (!inModel) continue

    const providerMatch = trimmed.match(/^\s*provider\s*:\s*["']?([^"'\s#]+)/)
    if (providerMatch) info.provider = providerMatch[1]

    const modelMatch = trimmed.match(/^\s*model\s*:\s*["']?([^"'\s#]+)/)
    if (modelMatch) info.model = modelMatch[1]

    const keyMatch = trimmed.match(/^\s*api_key\s*:\s*["']?([^"'#]*)/)
    if (keyMatch) info.api_key = keyMatch[1].trim()

    const urlMatch = trimmed.match(/^\s*base_url\s*:\s*["']?([^"'\s#]*)/)
    if (urlMatch) info.base_url = urlMatch[1]

    const visionMatch = trimmed.match(/^vision\s*:\s*["']?([^"'\s#]+)/)
    if (visionMatch) {
      const v = visionMatch[1].toLowerCase()
      info.vision = ['true', 'on', 'yes'].includes(v) ? 'on' : ['false', 'off', 'no'].includes(v) ? 'off' : 'auto'
    }
  }
  return info
}
