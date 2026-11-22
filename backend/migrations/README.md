# 初始结构

`src/models/tables.py` 定义初始关系结构。API 启动时显式创建演示库的表；此版本只包含初始 schema。需要在已有部署升级表结构时，应单独引入版本化迁移。

时段预约新增 `node_bandwidths`（各库可借带宽）与 `manifest_bookings`（清单预约）两张表。`create_all` 只会建缺失的表，已有部署升级到本版本后会自动补建；`manifest_bookings.active_key` 上的唯一约束保证同一清单最多一份有效预约，因此旧库需允许该列的唯一索引。
