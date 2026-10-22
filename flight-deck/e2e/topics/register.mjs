import { register } from 'node:module'
import { pathToFileURL } from 'node:url'
register('./loader.mjs', pathToFileURL('./'))
import './clock.mjs'
import './env.mjs'
