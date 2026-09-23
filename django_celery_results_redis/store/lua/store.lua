-- Atomic record operations shared by every driver.
--
-- ARGV[1] is a JSON payload with an "op" member. Optional members are omitted
-- rather than sent as null. When "index" is present, the secondary indexes of
-- the indexed driver are maintained in the same call:
--   index.prefix  key prefix of the model's indexes, ending with ":"
--   index.tags    fields indexed by value (sorted sets)
--   index.dates   fields indexed by score (sorted sets)
--   index.counts  fields whose distinct values are counted (hashes)
--   index.all     whether to keep the set of every primary key
--
-- Writes also keep "null_marker", a comma separated list of the "nullable"
-- fields that are absent, so that search indexes can find null values.

local STATUS_CREATED = 1
local STATUS_UPDATED = 0
local STATUS_EXISTS = -1
local STATUS_MISSING = -2

local function read_record(key, fields)
    local values = redis.call('HMGET', key, unpack(fields))
    local record = {}
    for i, field in ipairs(fields) do
        if values[i] then
            record[field] = values[i]
        end
    end
    return record, values
end

-- Primary keys are kept in sorted sets so that a window of them can be read
-- in order. Tag indexes score every member 0, which makes Redis order them by
-- primary key; date indexes score them by the datetime.

-- Distinct values are counted per field so that they can be listed without
-- reading the records. The counter of a value is dropped when it reaches zero.

local function count_value(idx, field, value, step)
    local key = idx.prefix .. 'c:' .. field
    local member = value == nil and 'n' or ('v' .. value)
    if redis.call('HINCRBY', key, member, step) <= 0 then
        redis.call('HDEL', key, member)
    end
end

local function unindex(idx, pk, record)
    if idx.all then
        redis.call('ZREM', idx.prefix .. 'all', pk)
    end
    for _, field in ipairs(idx.counts) do
        count_value(idx, field, record[field], -1)
    end
    for _, field in ipairs(idx.tags) do
        local value = record[field]
        if value == nil then
            redis.call('ZREM', idx.prefix .. 'n:' .. field, pk)
        else
            local members = idx.prefix .. 's:' .. field .. ':' .. value
            redis.call('ZREM', members, pk)
            if redis.call('ZCARD', members) == 0 then
                redis.call('SREM', idx.prefix .. 'v:' .. field, value)
            end
        end
    end
    for _, field in ipairs(idx.dates) do
        if record[field] == nil then
            redis.call('ZREM', idx.prefix .. 'n:' .. field, pk)
        else
            redis.call('ZREM', idx.prefix .. 'z:' .. field, pk)
        end
    end
end

local function reindex(idx, pk, record)
    if idx.all then
        redis.call('ZADD', idx.prefix .. 'all', 0, pk)
    end
    for _, field in ipairs(idx.counts) do
        count_value(idx, field, record[field], 1)
    end
    for _, field in ipairs(idx.tags) do
        local value = record[field]
        if value == nil then
            redis.call('ZADD', idx.prefix .. 'n:' .. field, 0, pk)
        else
            redis.call('ZADD', idx.prefix .. 's:' .. field .. ':' .. value, 0, pk)
            redis.call('SADD', idx.prefix .. 'v:' .. field, value)
        end
    end
    for _, field in ipairs(idx.dates) do
        local value = record[field]
        if value == nil then
            redis.call('ZADD', idx.prefix .. 'n:' .. field, 0, pk)
        else
            redis.call('ZADD', idx.prefix .. 'z:' .. field, value, pk)
        end
    end
end

local function purge_from_indexes(idx, pk)
    redis.call('ZREM', idx.prefix .. 'all', pk)
    for _, field in ipairs(idx.tags) do
        redis.call('ZREM', idx.prefix .. 'n:' .. field, pk)
        local values_key = idx.prefix .. 'v:' .. field
        for _, value in ipairs(redis.call('SMEMBERS', values_key)) do
            local members = idx.prefix .. 's:' .. field .. ':' .. value
            if redis.call('ZREM', members, pk) == 1 and redis.call('ZCARD', members) == 0 then
                redis.call('SREM', values_key, value)
            end
        end
    end
    for _, field in ipairs(idx.dates) do
        redis.call('ZREM', idx.prefix .. 'n:' .. field, pk)
        redis.call('ZREM', idx.prefix .. 'z:' .. field, pk)
    end
end

local function update_null_marker(key, marker, nullable)
    local nulls = {}
    for _, field in ipairs(nullable) do
        if redis.call('HEXISTS', key, field) == 0 then
            table.insert(nulls, field)
        end
    end
    if #nulls > 0 then
        redis.call('HSET', key, marker, table.concat(nulls, ','))
    else
        redis.call('HDEL', key, marker)
    end
end

local function write(p)
    local key = p.key
    local exists = redis.call('EXISTS', key) == 1
    if p.mode == 'insert' and exists then
        return {STATUS_EXISTS}
    end
    if p.mode == 'update' and not exists then
        return {STATUS_MISSING}
    end
    if p.index and exists then
        unindex(p.index, p.pk, (read_record(key, p.fields)))
    end
    for field, value in pairs(p.set or {}) do
        redis.call('HSET', key, field, value)
    end
    for _, field in ipairs(p.clear or {}) do
        redis.call('HDEL', key, field)
    end
    if not exists then
        for field, value in pairs(p.on_create or {}) do
            redis.call('HSET', key, field, value)
        end
    end
    update_null_marker(key, p.null_marker, p.nullable or {})
    if p.ttl then
        redis.call('EXPIRE', key, p.ttl)
    end
    local record, stored = read_record(key, p.fields)
    if p.index then
        reindex(p.index, p.pk, record)
    end
    if exists then
        return {STATUS_UPDATED, stored}
    end
    return {STATUS_CREATED, stored}
end

local function delete(p)
    local deleted = 0
    for i, key in ipairs(p.keys) do
        if p.index and redis.call('EXISTS', key) == 1 then
            unindex(p.index, p.pks[i], (read_record(key, p.fields)))
        end
        deleted = deleted + redis.call('DEL', key)
    end
    return deleted
end

local function rename(p)
    if redis.call('EXISTS', p.key) == 0 then
        return STATUS_MISSING
    end
    if redis.call('EXISTS', p.new_key) == 1 then
        return STATUS_EXISTS
    end
    if p.index then
        local record = (read_record(p.key, p.fields))
        unindex(p.index, p.pk, record)
        reindex(p.index, p.new_pk, record)
    end
    redis.call('RENAME', p.key, p.new_key)
    redis.call('HSET', p.new_key, p.pk_field, p.new_pk)
    for field, value in pairs(p.set or {}) do
        redis.call('HSET', p.new_key, field, value)
    end
    return STATUS_UPDATED
end

local function repair(p)
    local repaired = 0
    for i, key in ipairs(p.keys) do
        if redis.call('EXISTS', key) == 0 then
            purge_from_indexes(p.index, p.pks[i])
            repaired = repaired + 1
        end
    end
    return repaired
end

local function reindex_records(p)
    for i, key in ipairs(p.keys) do
        if redis.call('EXISTS', key) == 1 then
            reindex(p.index, p.pks[i], (read_record(key, p.fields)))
        end
    end
    return #p.keys
end

local function chord_part_return(p)
    if redis.call('EXISTS', p.key) == 0 then
        return false
    end
    local remaining = redis.call('HINCRBY', p.key, 'count', -1)
    if remaining ~= 0 then
        return {remaining}
    end
    local sub_tasks = redis.call('HGET', p.key, 'sub_tasks')
    redis.call('DEL', p.key)
    return {0, sub_tasks}
end

local operations = {
    write = write,
    delete = delete,
    rename = rename,
    repair = repair,
    reindex = reindex_records,
    chord_part_return = chord_part_return,
}

local payload = cjson.decode(ARGV[1])
return operations[payload.op](payload)
