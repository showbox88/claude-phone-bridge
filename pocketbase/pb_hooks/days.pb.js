/// <reference path="../pb_data/types.d.ts" />

// 替代 Notion 的 Amount(USD) formula
// 公式：rate 有值 → amount * rate；rate 空 → amount 本身（默认 USD）
// 四舍五入保留 2 位小数
//
// 注意：PB JS hook 每个 callback 是独立 VM，顶层 function 不共享，
// 所以 computeAmountUSD 必须内联到每个 hook 内。

onRecordCreate((e) => {
  const amount = e.record.getFloat("amount");
  const rate = e.record.getFloat("rate");
  if (!amount && !rate) {
    e.record.set("amount_usd", null);
  } else {
    const usd = rate > 0 ? amount * rate : amount;
    e.record.set("amount_usd", Math.round(usd * 100) / 100);
  }
  e.next();
}, "days");

onRecordUpdate((e) => {
  const amount = e.record.getFloat("amount");
  const rate = e.record.getFloat("rate");
  if (!amount && !rate) {
    e.record.set("amount_usd", null);
  } else {
    const usd = rate > 0 ? amount * rate : amount;
    e.record.set("amount_usd", Math.round(usd * 100) / 100);
  }
  e.next();
}, "days");
