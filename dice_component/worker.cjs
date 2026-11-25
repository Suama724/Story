const { createInterface } = require("node:readline");
const { DynamicLoader, Version } = require("bcdice");

async function main() {
  const engine = await new DynamicLoader().dynamicLoad("Cthulhu7th");
  const input = createInterface({ input: process.stdin });
  for await (const line of input) {
    try {
      const { expression, target, modifier, difficulty } = JSON.parse(line);
      let command;
      if (typeof expression === "string") {
        if (!/^(?:\d*d\d+|\d+)(?:[+-](?:\d*d\d+|\d+))*$/i.test(expression))
          throw new Error("bad expression");
        command = /d/i.test(expression) ? expression : `C(${expression})`;
      } else {
        if (!Number.isInteger(target) || target < 0 || target > 9999 ||
            !Number.isInteger(modifier) || Math.abs(modifier) > 2 ||
            !["", "h", "e"].includes(difficulty))
          throw new Error("bad check");
        command = `CC(${modifier})<=${target}${difficulty}`;
      }
      const result = engine.eval(command);
      if (!result) throw new Error("unsupported command");
      // 通用骰式的末段是纯数字总值。CoC 结果使用原始骰迹与布尔字段。
      const total = expression === undefined ? null : /(?:^|＞)\s*(-?\d+)\s*$/.exec(result.text);
      if (expression !== undefined && (!total || !Number.isSafeInteger(Number(total[1]))))
        throw new Error("missing total");
      process.stdout.write(JSON.stringify({
        version: Version, rands: result.detailedRands,
        total: total ? Number(total[1]) : null,
        critical: result.critical, fumble: result.fumble,
      }) + "\n");
    } catch {
      process.stdout.write('{"error":"evaluation_failed"}\n');
    }
  }
}

main().catch(() => { process.exitCode = 1; });
