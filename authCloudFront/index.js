'use strict'

exports.handler = (event, context, callback) => {
    // Get request and request headers
  const request = event.Records[0].cf.request
  const headers = request.headers

    // Configure authentication
  const authUser = USER_LOGIN
  const authPass = USER_PASS

    // Construct the Basic Auth string
  const authString = 'Basic ' + Buffer.from(authUser + ':' + authPass).toString('base64')

    // Require Basic authentication
  if (typeof headers.authorization === 'undefined' || headers.authorization[0].value !== authString) {
    const body = 'Unauthorized'
    const response = {
      status: '401',
      statusDescription: 'Unauthorized',
      body: body,
      headers: {
        'www-authenticate': [{key: 'WWW-Authenticate', value: 'Basic'}]
      }
    }
    callback(null, response)
  }

    // Continue request processing if authentication passed
  callback(null, request)
}
