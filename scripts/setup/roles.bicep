// Read-only roles for the FinOps agent identity on one subscription.
// Deploy once per subscription listed in finops/config.yaml:
//   az deployment sub create --subscription <id> -l <location> -f roles.bicep -p principalId=<principal-id>
//
// Reader: Resource Graph, Activity Log, Advisor, resource metadata.
// Cost Management Reader: cost queries. On EA and MCA billing, cost data also needs the billing-side
// setting that lets subscription readers see charges (see docs/setup.md).

targetScope = 'subscription'

@description('Object (principal) id of the managed identity from identity.bicep.')
param principalId string

var roles = {
  Reader: 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
  CostManagementReader: '72fafb9e-0641-4937-9268-a91bfd8191a3'
}

resource assignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for role in items(roles): {
    name: guid(subscription().id, principalId, role.value)
    properties: {
      principalId: principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', role.value)
      description: 'Azure FinOps agent (read-only): ${role.key}'
    }
  }
]

output roleAssignmentIds array = [for (role, i) in items(roles): assignments[i].id]
