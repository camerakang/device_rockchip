/** Fixed v0x22 UDP network binding. */
export const CONTROL_MULTICAST = '239.255.33.21';
export const CONTROL_PORT = 47821;
export const DISCOVERY_WINDOW_US = 100_000;
export const DEFAULT_TRANSACTION_TIMEOUT_MS = 30;

export type Endpoint = { address: string; port: number };
export type StaticEndpoint = Endpoint & { uid: bigint };

export function validateEndpoint(endpoint: StaticEndpoint) {
    if (typeof endpoint.uid !== 'bigint' || endpoint.uid <= 0n || endpoint.uid > 0xffffffffffffffffn) throw Error('UID范围');
    if (!endpoint.address || !Number.isInteger(endpoint.port) || endpoint.port < 1 || endpoint.port > 65535) {
        throw Error('网络endpoint无效');
    }
    return endpoint;
}

export const sameEndpoint = (a: Endpoint, b: Endpoint) => a.address === b.address && a.port === b.port;
