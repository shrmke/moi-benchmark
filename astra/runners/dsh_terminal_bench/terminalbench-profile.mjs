/** Apply benchmark-only request settings at DSH's native agent seams. */
export const name = 'terminalbench-profile'

export function apply(ctx, config = {}) {
  const maxSteps = config.maxSteps
  const temperature = config.temperature

  if (!Number.isSafeInteger(maxSteps) || maxSteps <= 0) {
    throw new TypeError('terminalbench-profile maxSteps must be a positive safe integer')
  }
  if (typeof temperature !== 'number' || !Number.isFinite(temperature)) {
    throw new TypeError('terminalbench-profile temperature must be finite')
  }

  ctx.on('agent/pre-step', (payload, next) => (
    payload.step > maxSteps ? { kind: 'reject' } : next()
  ))
  ctx.on('agent/request', async (_payload, next) => ({
    ...await next(),
    temperature,
  }))
}
