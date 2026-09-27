import { parentPort, workerData } from 'node:worker_threads';
import { MCPQueryService } from './query.mjs';

const service = new MCPQueryService(workerData.dataRoot);
parentPort.on('message', ({ id, name, args }) => {
  try {
    parentPort.postMessage({ id, result: service.callTool(name, args) });
  } catch (error) {
    parentPort.postMessage({ id, error: { code: error?.code, message: String(error?.message ?? '') } });
  }
});
