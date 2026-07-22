import requestAuthCode from 'dingtalk-jsapi/api/union/requestAuthCode'

export interface DingTalkAuthCodeRequest {
  clientId: string
  corpId: string
}

export async function requestDingTalkAuthCode({
  clientId,
  corpId,
}: DingTalkAuthCodeRequest): Promise<string> {
  const result = await requestAuthCode({ clientId, corpId })
  if (!result || typeof result.code !== 'string' || !result.code.trim()) {
    throw new Error('DingTalk did not return a login code')
  }
  return result.code.trim()
}
