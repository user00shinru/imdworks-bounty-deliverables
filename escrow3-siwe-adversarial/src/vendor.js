/**
 * Re-export the vendored ethers so every module imports one instance.
 * Off-line: `vendor/node_modules` ships inside this bundle, no npm install.
 */
export { ethers } from '../vendor/ethers/lib.commonjs/index.js';
