/*
 * Copyright (C) 2025 ScyllaDB
 *
 */

/*
 * SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
 */

#pragma once

#include "encryption.hh"

namespace encryption {

class azure_key_provider_factory : public key_provider_factory {
public:
    shared_ptr<key_provider> get_provider(encryption_context&, const options&) override;
};

/**
 * See comment for AWS KMS regarding system key support.
 */
}
