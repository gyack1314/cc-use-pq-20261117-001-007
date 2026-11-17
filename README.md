# LabTrace 实验室镜像采集服务

LabTrace 接收本地合成实验仪器镜像，按实验协议保存页级状态与原始观测，并提供质量、冲突和撤回后的当前交付。数据仅为演示，不用于医学判断。

使用 Python 3.11+，先在准备阶段安装 requirements.txt 的锁定依赖。启动服务：

python -m uvicorn labtrace.api:create_app --factory --host 127.0.0.1 --port 8000

本地验证：

python -m unittest discover -s tests -v
python -m compileall -q labtrace
python -m labtrace.demo

先向 /protocols 登记协议，再向 /instruments 登记仪器。批次清单使用相对页路径和每页 SHA-256，调用 /batches/ingest 提交清单路径。页缺失或摘要错误保留失败游标，文件修复后调用 /batches/resume。查询 /current、/history、/trace 和 /summary 检查交付与证据。快照通过 /snapshots 创建，通过 /snapshots/verify 校验摘要。
