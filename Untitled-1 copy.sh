#!/bin/bash

images=(
    "new-joiners"
    "entitlements"
    "policy"
    "sod-test"
    "identities"
    "peer-affinity"
)

for image in "${images[@]}"; do
  cd $image
  
  kubectl delete -f .
  echo "Deploying $image"
  kubectl apply -f .

  cd ../

done

echo "Completed!"