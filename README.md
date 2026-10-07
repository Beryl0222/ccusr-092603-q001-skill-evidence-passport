# 复合技能证据通行证

为院校、赛事与招聘机构交换能力证据定义稳定契约，保留标准版本和授权边界，并提供可重启恢复的业务服务。

## 目录

- `contracts/domain.schema.json`：事件信封、对象类型和事件载荷约定。
- `data/sample.json`：可直接校验的中文联调样例。
- `src/skill_evidence_passport/contracts.py`：交换契约校验。
- `src/skill_evidence_passport/service.py`：业务服务，覆盖幂等回执与同号异内容冻结、证据去重计权、并发签发唯一有效通行证、标准改版只重算受影响单元、录用快照差异说明、申诉、有限期共享许可与撤权传播、重启恢复，以及企业/学生双视图。
- `src/skill_evidence_passport/cli.py`：命令行校验入口。
- `tests/`：契约边界测试与业务服务场景测试。
- `docs/domain.md`：领域对象、事件语义与业务服务语义。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m skill_evidence_passport.cli contracts/domain.schema.json data/sample.json
```

命令成功时输出 `valid`；校验失败时逐行输出字段、代码和中文说明，并以非零状态结束。
